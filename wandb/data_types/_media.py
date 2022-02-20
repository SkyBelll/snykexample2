import hashlib
from typing import Optional, Sequence, Type, TYPE_CHECKING, Union
import os
import platform
import shutil
import tempfile

from _wandb_value import WBValue

from wandb._globals import _datatypes_callback
from wandb.util import (
    check_windows_valid_filename,
    mkdir_exists_ok,
    to_forward_slash_path,
)


if TYPE_CHECKING:
    from wandb_run import Run
    from wandb.sdk.wandb_artifacts import Artifact
    from wandb.apis.public import Artifact as PublicArtifact


SYS_PLATFORM = platform.system()


def _wb_filename(
    key: Union[str, int], step: Union[str, int], id: Union[str, int], extension: str
) -> str:
    return "{}_{}_{}{}".format(str(key), str(step), str(id), extension)


class Media(WBValue):
    """A WBValue that we store as a file outside JSON and show in a media panel
    on the front end.

    If necessary, we move or copy the file into the Run's media directory so that it gets
    uploaded.
    """

    _path: Optional[str]
    _run: Optional["Run"]
    _caption: Optional[str]
    _is_tmp: Optional[bool]
    _extension: Optional[str]
    _sha256: Optional[str]
    _size: Optional[int]

    # Staging directory so we can encode raw data into files, then hash them before
    # we put them into the Run directory to be uploaded.
    _MEDIA_TMP = tempfile.TemporaryDirectory("wandb-media")

    def __init__(self, caption: Optional[str] = None) -> None:
        super().__init__()
        self._path = None
        # The run under which this object is bound, if any.
        self._run = None
        self._caption = caption

    def _set_file(
        self, path: str, is_tmp: bool = False, extension: Optional[str] = None
    ) -> None:
        self._path = path
        self._is_tmp = is_tmp
        self._extension = extension
        if extension is not None and not path.endswith(extension):
            raise ValueError(
                'Media file extension "{}" must occur at the end of path "{}".'.format(
                    extension, path
                )
            )

        with open(self._path, "rb") as f:
            self._sha256 = hashlib.sha256(f.read()).hexdigest()
        self._size = os.path.getsize(self._path)

    @classmethod
    def get_media_subdir(cls: Type["Media"]) -> str:
        raise NotImplementedError

    @staticmethod
    def captions(
        media_items: Sequence["Media"],
    ) -> Union[bool, Sequence[Optional[str]]]:
        if media_items[0]._caption is not None:
            return [m._caption for m in media_items]
        else:
            return False

    def is_bound(self) -> bool:
        return self._run is not None

    def file_is_set(self) -> bool:
        return self._path is not None and self._sha256 is not None

    def bind_to_run(
        self,
        run: "Run",
        key: Union[int, str],
        step: Union[int, str],
        id_: Optional[Union[int, str]] = None,
    ) -> None:
        """Bind this object to a particular Run.

        Calling this function is necessary so that we have somewhere specific to
        put the file associated with this object, from which other Runs can
        refer to it.
        """
        if not self.file_is_set():
            raise AssertionError("bind_to_run called before _set_file")

        if SYS_PLATFORM == "Windows" and not check_windows_valid_filename(key):
            raise ValueError(
                f"Media {key} is invalid. Please remove invalid filename characters"
            )

        # The following two assertions are guaranteed to pass
        # by definition file_is_set, but are needed for
        # mypy to understand that these are strings below.
        assert isinstance(self._path, str)
        assert isinstance(self._sha256, str)

        if run is None:
            raise TypeError('Argument "run" must not be None.')
        self._run = run

        # Following assertion required for mypy
        assert self._run is not None

        if self._extension is None:
            _, extension = os.path.splitext(os.path.basename(self._path))
        else:
            extension = self._extension

        if id_ is None:
            id_ = self._sha256[:20]

        file_path = _wb_filename(key, step, id_, extension)
        media_path = os.path.join(self.get_media_subdir(), file_path)
        new_path = os.path.join(self._run.dir, media_path)
        mkdir_exists_ok(os.path.dirname(new_path))

        if self._is_tmp:
            shutil.move(self._path, new_path)
            self._path = new_path
            self._is_tmp = False
            _datatypes_callback(media_path)
        else:
            shutil.copy(self._path, new_path)
            self._path = new_path
            _datatypes_callback(media_path)

    def to_json(self, run: Union["Run", "Artifact"]) -> dict:
        """Serializes the object into a JSON blob, using a run or artifact to store additional data. If `run_or_artifact`
        is a wandb.Run then `self.bind_to_run()` must have been previously been called.

        Args:
            run_or_artifact (wandb.Run | wandb.Artifact): the Run or Artifact for which this object should be generating
            JSON for - this is useful to to store additional data if needed.

        Returns:
            dict: JSON representation
        """
        # NOTE: uses of Audio in this class are a temporary hack -- when Ref support moves up
        # into Media itself we should get rid of them
        from wandb.data_types import Audio

        json_obj = {}
        if isinstance(run, Run):
            if not self.is_bound():
                raise RuntimeError(
                    "Value of type {} must be bound to a run with bind_to_run() before being serialized to JSON.".format(
                        type(self).__name__
                    )
                )

            assert (
                self._run is run
            ), "We don't support referring to media files across runs."

            # The following two assertions are guaranteed to pass
            # by definition is_bound, but are needed for
            # mypy to understand that these are strings below.
            assert isinstance(self._path, str)

            json_obj.update(
                {
                    "_type": "file",  # TODO(adrian): This isn't (yet) a real media type we support on the frontend.
                    "path": to_forward_slash_path(
                        os.path.relpath(self._path, self._run.dir)
                    ),
                    "sha256": self._sha256,
                    "size": self._size,
                }
            )
            artifact_entry_url = self._get_artifact_entry_ref_url()
            if artifact_entry_url is not None:
                json_obj["artifact_path"] = artifact_entry_url
            artifact_entry_latest_url = self._get_artifact_entry_latest_ref_url()
            if artifact_entry_latest_url is not None:
                json_obj["_latest_artifact_path"] = artifact_entry_latest_url
        elif isinstance(run, Artifact):
            if self.file_is_set():
                # The following two assertions are guaranteed to pass
                # by definition of the call above, but are needed for
                # mypy to understand that these are strings below.
                assert isinstance(self._path, str)
                assert isinstance(self._sha256, str)
                artifact = run  # Checks if the concrete image has already been added to this artifact
                name = artifact.get_added_local_path_name(self._path)
                if name is None:
                    if self._is_tmp:
                        name = os.path.join(
                            self.get_media_subdir(), os.path.basename(self._path)
                        )
                    else:
                        # If the files is not temporary, include the first 8 characters of the file's SHA256 to
                        # avoid name collisions. This way, if there are two images `dir1/img.png` and `dir2/img.png`
                        # we end up with a unique path for each.
                        name = os.path.join(
                            self.get_media_subdir(),
                            self._sha256[:20],
                            os.path.basename(self._path),
                        )

                    # if not, check to see if there is a source artifact for this object
                    if (
                        self._artifact_source
                        is not None
                        # and self._artifact_source.artifact != artifact
                    ):
                        default_root = self._artifact_source.artifact._default_root()
                        # if there is, get the name of the entry (this might make sense to move to a helper off artifact)
                        if self._path.startswith(default_root):
                            name = self._path[len(default_root) :]
                            name = name.lstrip(os.sep)

                        # Add this image as a reference
                        path = self._artifact_source.artifact.get_path(name)
                        artifact.add_reference(path.ref_url(), name=name)
                    elif isinstance(self, Audio) and Audio.path_is_reference(
                        self._path
                    ):
                        artifact.add_reference(self._path, name=name)
                    else:
                        entry = artifact.add_file(
                            self._path, name=name, is_tmp=self._is_tmp
                        )
                        name = entry.path

                json_obj["path"] = name
                json_obj["sha256"] = self._sha256
            json_obj["_type"] = self._log_type
        return json_obj

    @classmethod
    def from_json(
        cls: Type["Media"], json_obj: dict, source_artifact: "PublicArtifact"
    ) -> "Media":
        """Likely will need to override for any more complicated media objects"""
        return cls(source_artifact.get_path(json_obj["path"]).download())

    def __eq__(self, other: object) -> bool:
        """Likely will need to override for any more complicated media objects"""
        return (
            isinstance(other, self.__class__)
            and hasattr(self, "_sha256")
            and hasattr(other, "_sha256")
            and self._sha256 == other._sha256
        )