# SPDX-FileCopyrightText: 2021-2024 Greenbone AG
#
# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Optional

from gnupg import GPG

OPENVAS_GPG_HOME = "/etc/openvas/gnupg"


logger = logging.getLogger(__name__)


def __determine_default_gpg_home() -> Path:
    gos_default = Path(OPENVAS_GPG_HOME)
    if gos_default.exists():
        return gos_default
    user_default = Path.home() / ".gnupg"
    if not user_default.exists():
        logger.warning(
            "No GnuPG home found; "
            "please verify setup and set the GNUPGHOME variable if necessary"
        )
    return user_default


def __default_gpg_home() -> GPG:
    """
    __defaultGpgHome tries to load the variable 'GNUPGHOME' or to guess it
    """
    manual = os.getenv("GNUPGHOME")

    home = Path(manual) if manual else __determine_default_gpg_home()
    logger.debug("Using %s as GnuPG home.", home)
    return GPG(gnupghome=f"{home.absolute()}")


@dataclass
class ReloadConfiguration:
    hash_file: Path
    on_verification_failure: Callable[
        [Optional[Dict[str, str]]], Dict[str, str]
    ]
    gpg: Optional[GPG] = None
    cache: Optional[Dict[str, str]] = None
    fingerprint: str = ""


def reload_sha256sums(
    config: ReloadConfiguration,
) -> Callable[[], Dict[str, str]]:
    """
    reload_sha256sums reloads sha256sums if a threshold has been reached.
    """
    if not config.gpg:
        config.gpg = __default_gpg_home()

    def create_hash(file: Path) -> str:
        # we just use the hash to identify we have to reload the sha256sums
        # therefore a collision is not the end of the world and sha1 is more
        # than sufficient
        hasher = hashlib.sha1()
        with file.open(mode="rb") as f:
            for hash_file_bytes in iter(lambda: f.read(1024), b""):
                hasher.update(hash_file_bytes)
        return hasher.hexdigest()

    def internal_reload() -> Dict[str, str]:
        fingerprint = create_hash(config.hash_file)
        if not config.cache or config.fingerprint != fingerprint:
            config.fingerprint = fingerprint
            config.cache = gpg_sha256sums(config.hash_file, config.gpg)
        if not config.cache:
            return config.on_verification_failure(None)
        return config.cache

    return internal_reload


def gpg_sha256sums(
    hash_file: Path, gpg: Optional[GPG] = None
) -> Optional[Dict[str, str]]:
    """
    gpg_sha256sums verifies given hash_file with a asc file

    This functions assumes that the asc file is in the same directory as the
    hashfile and has the same name but with the suffix '.asc'
    """

    # when doing that via paramater list it is loading eagerly on import
    # which may fail on some systems
    if not gpg:
        gpg = __default_gpg_home()
    asc_path = hash_file.parent / f"{hash_file.name}.asc"
    with asc_path.open(mode="rb") as f:
        verified = gpg.verify_file(f, str(hash_file.absolute()))
        if not verified:
            return None
        result = {}
        with hash_file.open() as f:  # noqa: PLW2901
            for line in f.readlines():
                hsum, fname = line.split("  ")
                # the second part can contain a newline
                # sometimes the hash sum got generated outside the current dir
                # and may contain leading paths.
                # Since we check against the filename we should normalize to
                # prevent false positives.
                result[hsum] = fname.split("/")[-1].strip()
        return result


class VerificationResult(Enum):
    INVALID_FILE = 0
    INVALID_HASH = 1
    INVALID_NAME = 2
    SUCCESS = 3


def create_verify(
    sha256sums: Callable[[], Dict[str, str]],
) -> Callable[[Path], VerificationResult]:
    """
    create_verify is returning a closure based on the sha256sums.

    This allows to load sha256sums and verify there instead of verifying and
    loading on each verification request.
    """

    def verify(advisory_path: Path) -> VerificationResult:
        s256h = hashlib.sha256()
        if not advisory_path.is_file():
            return VerificationResult.INVALID_FILE

        with advisory_path.open(mode="rb") as f:
            for hash_file_bytes in iter(lambda: f.read(1024), b""):
                s256h.update(hash_file_bytes)
        hash_sum = s256h.hexdigest()

        assumed_name = sha256sums().get(hash_sum)
        if not assumed_name:
            return VerificationResult.INVALID_HASH
        return (
            VerificationResult.SUCCESS
            if assumed_name == advisory_path.name
            else VerificationResult.INVALID_NAME
        )

    return verify
