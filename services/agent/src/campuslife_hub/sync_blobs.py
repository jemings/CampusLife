"""Blobs over the sync port (#4, D7, E19): uploads through the BlobStore's sessions, and downloads.

- POST /v1/blobs/uploads opens or resumes the Device's upload of one Blob: 200 blob/stored when the Hub already
  stores it, else 201 blob/upload_session with the chunk size the Hub chose and the chunks received. Asking again is
  how a client learns where an upload stands.
- PUT /v1/blobs/uploads/{sha}/chunks/{i} stores one chunk, in any order. The body must be exactly that chunk's length:
  a Content-Length that says otherwise is refused before the body is read, and a body without one is read at most one
  byte past it (#4 decision 5). The index is written plainly, 0 or a number without a leading zero (#4 decision 11).
- POST /v1/blobs/uploads/{sha}/finalize combines the chunks and stores the Blob once its sha256 and size check out.
- GET /v1/blobs/{sha} sends the Blob whole, or the one byte range Range names (206); a range starting past the end is
  416 with the size. The ETag is the sha256, so an If-None-Match naming it is 304 whatever the Range. Starlette's
  FileResponse would tag by md5, answer two ranges with multipart and send no 304, so the bytes are sent here.

A refusal before the whole body is read leaves the rest to the server, which reads and drops it, so the connection
stays usable (uvicorn's h11 protocol discards body data after the response; #4 decision 5). A request sent with
`Expect: 100-continue` and refused that early is the client's to settle: it sends the body it declared on that
connection or closes it. uvicorn keeps waiting for the declared body and reads a next request sent without it as
that body: when the declared length is longer than that request, as with an 8 MiB chunk, nothing answers it until the
keep-alive timeout closes the connection; when it is shorter, a 400 text/plain outside the contract answers and the
connection closes. No
`Connection: close` is added to these refusals: a client that sent no Expect may already be writing the body, and
closing on it risks a reset before it reads the answer.
"""

from __future__ import annotations

import contextlib
import re
from typing import BinaryIO

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from .blobs import BlobStore, ChunkMismatch, HashMismatch, HubStorageFull, Incomplete, InvalidUpload, NoUploadSession, StoredBlob, UploadRefused
from .sync_envelope import ErrorCode, SyncError, device_of, judge, read_capped, read_json

CHUNK_INDEX = re.compile(r"0|[1-9][0-9]{0,18}")
BYTE_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)")
DOWNLOAD_PIECE = 64 * 1024
OCTETS = "application/octet-stream"

STATUS_OF: dict[type[UploadRefused], int] = {
    InvalidUpload: 400, HubStorageFull: 400, NoUploadSession: 404, ChunkMismatch: 409, Incomplete: 409, HashMismatch: 409,
}
"""The status sync.yaml gives each refusal of the Blob store."""

router = APIRouter()


def _refused(error: UploadRefused) -> SyncError:
    return SyncError(STATUS_OF[type(error)], ErrorCode(error.code), str(error), details=error.details)


def _blobs(request: Request) -> BlobStore:
    return request.app.state.sync.hosted.core.blobs


def _sha(sha: str) -> str:
    judge("common/sha256", sha)
    return sha


def _index(text: str) -> int:
    if not CHUNK_INDEX.fullmatch(text):
        raise SyncError(400, ErrorCode.INVALID_REQUEST, f"chunk {text!r} is not a chunk index: 0, or a number without a leading zero")
    return int(text)


def _declared_length(request: Request) -> int | None:
    declared = request.headers.get("content-length")
    return None if declared is None else int(declared)


@router.post("/v1/blobs/uploads")
async def open_upload(request: Request) -> JSONResponse:
    body = await read_json(request)
    judge("blob/upload_request", body)
    try:
        opened = await _blobs(request).open_upload(device_of(request), body["sha256"], body["size"])
    except UploadRefused as error:
        raise _refused(error) from None
    return JSONResponse(opened.body(), status_code=200 if isinstance(opened, StoredBlob) else 201)


@router.put("/v1/blobs/uploads/{sha}/chunks/{index}")
async def put_chunk(request: Request, sha: str, index: str) -> Response:
    sha, chunk, device, blobs = _sha(sha), _index(index), device_of(request), _blobs(request)
    try:
        length = await blobs.chunk_length(device, sha, chunk)
        if (declared := _declared_length(request)) is not None and declared != length:
            raise SyncError(400, ErrorCode.INVALID_REQUEST, f"chunk {chunk} is {length} bytes, not {declared}")
        data = await read_capped(request, length)
        if data is None or len(data) != length:
            raise SyncError(400, ErrorCode.INVALID_REQUEST, f"chunk {chunk} is {length} bytes")
        await blobs.put_chunk(device, sha, chunk, data)
    except UploadRefused as error:
        raise _refused(error) from None
    return Response(status_code=204)


@router.post("/v1/blobs/uploads/{sha}/finalize")
async def finalize_upload(request: Request, sha: str) -> JSONResponse:
    try:
        stored = await _blobs(request).finalize(device_of(request), _sha(sha))
    except UploadRefused as error:
        raise _refused(error) from None
    return JSONResponse(stored.body())


class _BlobBytes(Response):
    """`length` bytes of an open Blob file from byte `first`, read off the event loop.

    Sending stops when the client goes, since uvicorn then drops what is sent without saying so, and the file closes
    however the sending ends: on Windows an open handle keeps a purge from removing the file.
    """

    def __init__(self, handle: BinaryIO, first: int, length: int, *, status_code: int, headers: dict[str, str]) -> None:
        super().__init__(status_code=status_code, headers={**headers, "Content-Length": str(length)}, media_type=OCTETS)
        self._handle, self._first, self._length = handle, first, length

    async def _send_bytes(self, send: Send) -> None:
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        await anyio.to_thread.run_sync(self._handle.seek, self._first)
        remaining = self._length
        while remaining:
            piece = await anyio.to_thread.run_sync(self._handle.read, min(DOWNLOAD_PIECE, remaining))
            if not piece:
                raise OSError(f"the Blob file ended {remaining} bytes short of its recorded size")
            remaining -= len(piece)
            await send({"type": "http.response.body", "body": piece, "more_body": True})
        await send({"type": "http.response.body", "body": b""})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with anyio.create_task_group() as sending:

                async def send_then_stop() -> None:
                    await self._send_bytes(send)
                    sending.cancel_scope.cancel()

                sending.start_soon(send_then_stop)
                while (await receive())["type"] != "http.disconnect":
                    pass
                sending.cancel_scope.cancel()
        finally:
            self._handle.close()


def _download_headers(request: Request) -> dict[str, str]:
    asked = {}
    for name in ("Range", "If-None-Match"):
        values = request.headers.getlist(name)
        if len(values) > 1:
            raise SyncError(400, ErrorCode.INVALID_REQUEST, f"send {name} once")
        if values:
            asked[name] = values[0]
    judge("blob/download_request_headers", asked)
    return asked


def _span(asked: str, size: int) -> tuple[int, int]:
    """The first byte and the length of the range asked for, which the contract has already judged one range."""
    first_text, last_text = BYTE_RANGE.fullmatch(asked).groups()  # type: ignore[union-attr]
    if first_text:
        first, last = int(first_text), min(int(last_text), size - 1) if last_text else size - 1
    else:
        first, last = size - min(int(last_text), size), size - 1
    if first >= size:
        raise SyncError(416, ErrorCode.INVALID_REQUEST, f"{asked} starts past the end of the {size} bytes", headers={"Content-Range": f"bytes */{size}"})
    return first, last - first + 1


@router.get("/v1/blobs/{sha}")
async def download(request: Request, sha: str) -> Response:
    sha, asked = _sha(sha), _download_headers(request)
    opened = await _blobs(request).open_blob(sha)
    if opened is None:
        raise SyncError(404, ErrorCode.NOT_FOUND, f"the Hub does not store {sha}")
    stored, handle = opened
    with contextlib.ExitStack() as owned:
        owned.callback(handle.close)
        tags = {"ETag": f'"{stored.sha256}"'}
        if asked.get("If-None-Match") == tags["ETag"]:
            return Response(status_code=304, headers=tags)
        tags["Accept-Ranges"] = "bytes"
        if "Range" not in asked:
            owned.pop_all()
            return _BlobBytes(handle, 0, stored.size, status_code=200, headers=tags)
        first, length = _span(asked["Range"], stored.size)
        owned.pop_all()
        return _BlobBytes(handle, first, length, status_code=206, headers={**tags, "Content-Range": f"bytes {first}-{first + length - 1}/{stored.size}"})
