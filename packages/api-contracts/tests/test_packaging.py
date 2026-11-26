import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build the wheel")


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out), str(PACKAGE_ROOT)],
        check=True,
        capture_output=True,
    )
    (built,) = out.glob("*.whl")
    return built


def test_wheel_carries_schemas_and_fixtures_as_package_data(wheel):
    names = set(zipfile.ZipFile(wheel).namelist())
    assert "campuslife_contracts/contract/schemas/common/uuid.json" in names
    assert any(name.startswith("campuslife_contracts/contract/fixtures/valid/") for name in names)
    assert "campuslife_contracts/vocabulary.json" in names


def test_installed_wheel_validates_from_its_bundled_schemas(wheel, tmp_path):
    probe = (
        "import campuslife_contracts as c;"
        "print(c.validate('common/uuid', 'NOPE'))"
    )
    result = subprocess.run(
        ["uv", "run", "--isolated", "--no-project", "--python", sys.executable, "--with", str(wheel),
         "python", "-c", probe],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert "SchemaViolation(path='', keyword='pattern')" in result.stdout
