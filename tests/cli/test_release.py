"""`belay release sign`/`belay release verify` (plan-v2 E19.6) -- Ed25519
authenticity signing of an offline release bundle, reusing the same
SigningKey/verify_signature primitives `belay keygen`/`verify-evidence`
(plan-v2 E13) already use, not a second signing scheme.
"""

from __future__ import annotations

from pathlib import Path

from belay.cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def _keygen(tmp_path: Path) -> Path:
    key_path = tmp_path / "signing.key"
    result = runner.invoke(app, ["keygen", str(key_path)])
    assert result.exit_code == 0, result.output
    return key_path


def _dist_dir(tmp_path: Path) -> Path:
    dist = tmp_path / "dist-bin"
    dist.mkdir()
    (dist / "belay.exe").write_bytes(b"fake windows binary contents")
    (dist / "belay").write_bytes(b"fake unix binary contents")
    return dist


def test_sign_writes_sums_signature_and_pubkey(tmp_path: Path) -> None:
    key_path = _keygen(tmp_path)
    dist = _dist_dir(tmp_path)

    result = runner.invoke(app, ["release", "sign", str(dist), "--key", str(key_path)])
    assert result.exit_code == 0, result.output

    assert (dist / "SHA256SUMS.txt").is_file()
    assert (dist / "SHA256SUMS.txt.sig").is_file()
    assert (dist / "release.pub").is_file()

    sums_text = (dist / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert "belay.exe" in sums_text
    assert "belay" in sums_text


def test_sign_then_verify_roundtrip_succeeds(tmp_path: Path) -> None:
    key_path = _keygen(tmp_path)
    dist = _dist_dir(tmp_path)
    assert runner.invoke(app, ["release", "sign", str(dist), "--key", str(key_path)]).exit_code == 0

    result = runner.invoke(app, ["release", "verify", str(dist)])
    assert result.exit_code == 0, result.output
    assert "signature: OK" in result.output
    assert "MISMATCH" not in result.output
    assert "MISSING" not in result.output


def test_verify_with_explicit_trusted_pubkey(tmp_path: Path) -> None:
    key_path = _keygen(tmp_path)
    dist = _dist_dir(tmp_path)
    assert runner.invoke(app, ["release", "sign", str(dist), "--key", str(key_path)]).exit_code == 0

    result = runner.invoke(
        app, ["release", "verify", str(dist), "--pubkey", str(key_path) + ".pub"]
    )
    assert result.exit_code == 0, result.output
    assert "signature: OK" in result.output


def test_verify_detects_a_tampered_file_after_signing(tmp_path: Path) -> None:
    key_path = _keygen(tmp_path)
    dist = _dist_dir(tmp_path)
    assert runner.invoke(app, ["release", "sign", str(dist), "--key", str(key_path)]).exit_code == 0

    (dist / "belay.exe").write_bytes(b"tampered contents after signing")

    result = runner.invoke(app, ["release", "verify", str(dist)])
    assert result.exit_code != 0
    assert "MISMATCH:" in result.output
    assert "belay.exe" in result.output
    # the signature itself is still valid (SHA256SUMS.txt wasn't touched) --
    # only the per-file hash check should fail, not silently claim OK
    assert "signature: OK" in result.output


def test_verify_detects_a_missing_file(tmp_path: Path) -> None:
    key_path = _keygen(tmp_path)
    dist = _dist_dir(tmp_path)
    assert runner.invoke(app, ["release", "sign", str(dist), "--key", str(key_path)]).exit_code == 0

    (dist / "belay").unlink()

    result = runner.invoke(app, ["release", "verify", str(dist)])
    assert result.exit_code != 0
    assert "MISSING:" in result.output


def test_verify_detects_a_wrong_public_key(tmp_path: Path) -> None:
    key_path = _keygen(tmp_path)
    dist = _dist_dir(tmp_path)
    assert runner.invoke(app, ["release", "sign", str(dist), "--key", str(key_path)]).exit_code == 0

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_key_path = _keygen(other_dir)

    result = runner.invoke(
        app, ["release", "verify", str(dist), "--pubkey", str(other_key_path) + ".pub"]
    )
    assert result.exit_code != 0
    assert "signature: INVALID" in result.output


def test_verify_detects_a_tampered_sums_file(tmp_path: Path) -> None:
    """A SHA256SUMS.txt that no longer matches what was actually signed
    (e.g. an attacker appended a line for a malicious extra file, or edited
    a hash to match a swapped-in file) must fail signature verification --
    the whole point of signing the sums file, not just the individual
    files."""
    key_path = _keygen(tmp_path)
    dist = _dist_dir(tmp_path)
    assert runner.invoke(app, ["release", "sign", str(dist), "--key", str(key_path)]).exit_code == 0

    sums_path = dist / "SHA256SUMS.txt"
    sums_path.write_text(sums_path.read_text(encoding="utf-8") + "\nextra line\n", encoding="utf-8")

    result = runner.invoke(app, ["release", "verify", str(dist)])
    assert result.exit_code != 0
    assert "signature: INVALID" in result.output


def test_sign_on_empty_directory_errors_cleanly(tmp_path: Path) -> None:
    key_path = _keygen(tmp_path)
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    result = runner.invoke(app, ["release", "sign", str(empty_dir), "--key", str(key_path)])
    assert result.exit_code != 0
    assert "no files found" in result.output


def test_verify_on_unsigned_directory_errors_cleanly(tmp_path: Path) -> None:
    dist = _dist_dir(tmp_path)
    result = runner.invoke(app, ["release", "verify", str(dist)])
    assert result.exit_code != 0
    assert "SHA256SUMS.txt" in result.output
