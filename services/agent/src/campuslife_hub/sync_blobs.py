"""Blob uploads over the sync port (#4, D7, E19): the HTTP side of the BlobStore's upload sessions.

- POST /v1/blobs/uploads opens or resumes the Device's upload of one Blob: 200 blob/stored when the Hub already
  stores it, else 201 blob/upload_session with the chunk size the Hub chose and the chunks received. Asking again is
  how a client learns where an upload stands.
- PUT /v1/blobs/uploads/{sha}/chunks/{i} stores one chunk, in any order. The body must be exactly that chunk's length:
  a Content-Length that says otherwise is refused before the body is read, and a body without one is read at most one
  byte past it (#4 decision 5). The index is written plainly, 0 or a number without a leading zero (#4 decision 11).
- POST /v1/blobs/uploads/{sha}/finalize combines the chunks and stores the Blob once its sha256 and size check out.

A refusal before the whole body is read leaves the rest to the server, which reads and drops it, so the connection
stays usable (uvicorn's h11 protocol discards body data after the response).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .blobs import BlobStore, ChunkMismatch, HashMismatch, HubStorageFull, Incomplete, InvalidUpload, NoUploadSession, StoredBlob, UploadRefused
from .sync_envelope import ErrorCode, SyncError, device_of, judge, read_capped, read_json

CHUNK_INDEX = re.compile(r"0|[1-9][0-9]{0,18}")

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
