from __future__ import annotations

import abc
import json
import zipfile
import os
import threading

from typing import ClassVar, Dict, List, Literal, Tuple, Any, Optional, Union, BinaryIO

import bsdiff4

semaphore = threading.Semaphore(os.cpu_count() or 4)

del threading
del os


class AutoPatchRegister(abc.ABCMeta):
    patch_types: ClassVar[Dict[str, AutoPatchRegister]] = {}
    file_endings: ClassVar[Dict[str, AutoPatchRegister]] = {}

    def __new__(mcs, name: str, bases: Tuple[type, ...], dct: Dict[str, Any]) -> AutoPatchRegister:
        # construct class
        new_class = super().__new__(mcs, name, bases, dct)
        if "game" in dct:
            AutoPatchRegister.patch_types[dct["game"]] = new_class
            if not dct["patch_file_ending"]:
                raise Exception(f"Need an expected file ending for {name}")
            AutoPatchRegister.file_endings[dct["patch_file_ending"]] = new_class
        return new_class

    @staticmethod
    def get_handler(file: str) -> Optional[AutoPatchRegister]:
        for file_ending, handler in AutoPatchRegister.file_endings.items():
            if file.endswith(file_ending):
                return handler
        return None


container_version: int = 6


class InvalidDataError(Exception):
    """
    Since games can override `read_contents` in APContainer,
    this is to report problems in that process.
    """


class APContainer:
    """A zipfile containing at least archipelago.json"""
    version: int = container_version
    compression_level: int = 9
    compression_method: int = zipfile.ZIP_DEFLATED
    game: Optional[str] = None

    # instance attributes:
    path: Optional[str]
    player: Optional[int]
    player_name: str
    server: str

    def __init__(self, path: Optional[str] = None, player: Optional[int] = None,
                 player_name: str = "", server: str = ""):
        self.path = path
        self.player = player
        self.player_name = player_name
        self.server = server

    def write(self, file: Optional[Union[str, BinaryIO]] = None) -> None:
        zip_file = file if file else self.path
        if not zip_file:
            raise FileNotFoundError(f"Cannot write {self.__class__.__name__} due to no path provided.")
        with semaphore:  # TODO: remove semaphore once generate_output has a thread limit
            with zipfile.ZipFile(
                    zip_file, "w", self.compression_method, True, self.compression_level) as zf:
                if file:
                    self.path = zf.filename
                self.write_contents(zf)

    def write_contents(self, opened_zipfile: zipfile.ZipFile) -> None:
        manifest = self.get_manifest()
        try:
            manifest_str = json.dumps(manifest)
        except Exception as e:
            raise Exception(f"Manifest {manifest} did not convert to json.") from e
        else:
            opened_zipfile.writestr("archipelago.json", manifest_str)

    def read(self, file: Optional[Union[str, BinaryIO]] = None) -> None:
        """Read data into patch object. file can be file-like, such as an outer zip file's stream."""
        zip_file = file if file else self.path
        if not zip_file:
            raise FileNotFoundError(f"Cannot read {self.__class__.__name__} due to no path provided.")
        with zipfile.ZipFile(zip_file, "r") as zf:
            if file:
                self.path = zf.filename
            try:
                self.read_contents(zf)
            except Exception as e:
                message = ""
                if len(e.args):
                    arg0 = e.args[0]
                    if isinstance(arg0, str):
                        message = f"{arg0} - "
                raise InvalidDataError(f"{message}This might be the incorrect world version for this file") from e

    def read_contents(self, opened_zipfile: zipfile.ZipFile) -> None:
        with opened_zipfile.open("archipelago.json", "r") as f:
            manifest = json.load(f)
        if manifest["compatible_version"] > self.version:
            raise Exception(f"File (version: {manifest['compatible_version']}) too new "
                            f"for this handler (version: {self.version})")
        self.player = manifest["player"]
        self.server = manifest["server"]
        self.player_name = manifest["player_name"]

    def get_manifest(self) -> Dict[str, Any]:
        return {
            "server": self.server,  # allow immediate connection to server in multiworld. Empty string otherwise
            "player": self.player,
            "player_name": self.player_name,
            "game": self.game,
            # minimum version of patch system expected for patching to be successful
            "compatible_version": 5,
            "version": container_version,
        }


class APPatch(APContainer):
    """
    An `APContainer` that represents a patch file.
    It includes the `procedure` key in the manifest to indicate that it is a patch.

    Your implementation should inherit from this if your output file
    represents a patch file, but will not be applied with AP's `Patch.py`
    """
    procedure: Union[Literal["custom"], List[Tuple[str, List[Any]]]] = "custom"

    def get_manifest(self) -> Dict[str, Any]:
        manifest = super(APPatch, self).get_manifest()
        manifest["procedure"] = self.procedure
        manifest["compatible_version"] = 6
        return manifest


class APAutoPatchInterface(APPatch, abc.ABC, metaclass=AutoPatchRegister):
    """
    An abstract `APPatch` that defines the requirements for a patch
    to be applied with AP's `Patch.py`
    """
    result_file_ending: str = ".sfc"

    @abc.abstractmethod
    def patch(self, target: str) -> None:
        """ create the output file with the file name `target` """


class APDeltaPatch(APAutoPatchInterface):
    """An implementation of `APAutoPatchInterface` that additionally
    has delta.bsdiff4 containing a delta patch to get the desired file."""

    hash: Optional[str]  # base checksum of source file
    patch_file_ending: str = ""
    delta: Optional[bytes] = None
    source_data: bytes
    procedure = None  # delete this line when APPP is added

    def __init__(self, *args: Any, patched_path: str = "", **kwargs: Any) -> None:
        self.patched_path = patched_path
        super(APDeltaPatch, self).__init__(*args, **kwargs)

    def get_manifest(self) -> Dict[str, Any]:
        manifest = super(APDeltaPatch, self).get_manifest()
        manifest["base_checksum"] = self.hash
        manifest["result_file_ending"] = self.result_file_ending
        manifest["patch_file_ending"] = self.patch_file_ending
        manifest["compatible_version"] = 5  # delete this line when APPP is added
        return manifest

    @classmethod
    def get_source_data(cls) -> bytes:
        """Get Base data"""
        raise NotImplementedError()

    @classmethod
    def get_source_data_with_cache(cls) -> bytes:
        if not hasattr(cls, "source_data"):
            cls.source_data = cls.get_source_data()
        return cls.source_data

    def write_contents(self, opened_zipfile: zipfile.ZipFile):
        super(APDeltaPatch, self).write_contents(opened_zipfile)
        # write Delta
        opened_zipfile.writestr("delta.bsdiff4",
                                bsdiff4.diff(self.get_source_data_with_cache(), open(self.patched_path, "rb").read()),
                                compress_type=zipfile.ZIP_STORED)  # bsdiff4 is a format with integrated compression

    def read_contents(self, opened_zipfile: zipfile.ZipFile):
        super(APDeltaPatch, self).read_contents(opened_zipfile)
        self.delta = opened_zipfile.read("delta.bsdiff4")

    def patch(self, target: str):
        """Base + Delta -> Patched"""
        if not self.delta:
            self.read()
        result = bsdiff4.patch(self.get_source_data_with_cache(), self.delta)
        with open(target, "wb") as f:
            f.write(result)
