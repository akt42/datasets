# Copyright 2020 The HuggingFace Datasets Authors and the TensorFlow Datasets Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Some python utils function and classes.

"""

import contextlib
import copy
import functools
import itertools
import os
import re
import types
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from io import BytesIO as StringIO
from multiprocessing import Pool, RLock
from shutil import disk_usage
from types import CodeType, FunctionType
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import dill
import numpy as np
from packaging import version
from tqdm.auto import tqdm

from .. import config
from . import logging


try:  # pragma: no branch
    import typing_extensions as _typing_extensions
    from typing_extensions import Final, Literal
except ImportError:
    _typing_extensions = Literal = Final = None


logger = logging.get_logger(__name__)


# NOTE: When used on an instance method, the cache is shared across all
# instances and IS NOT per-instance.
# See
# https://stackoverflow.com/questions/14946264/python-lru-cache-decorator-per-instance
# For @property methods, use @memoized_property below.
memoize = functools.lru_cache


def size_str(size_in_bytes):
    """Returns a human readable size string.

    If size_in_bytes is None, then returns "Unknown size".

    For example `size_str(1.5 * datasets.units.GiB) == "1.50 GiB"`.

    Args:
        size_in_bytes: `int` or `None`, the size, in bytes, that we want to
            format as a human-readable size string.
    """
    if not size_in_bytes:
        return "Unknown size"

    _NAME_LIST = [("PiB", 2**50), ("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)]

    size_in_bytes = float(size_in_bytes)
    for (name, size_bytes) in _NAME_LIST:
        value = size_in_bytes / size_bytes
        if value >= 1.0:
            return f"{value:.2f} {name}"
    return f"{int(size_in_bytes)} bytes"


def convert_file_size_to_int(size: Union[int, str]) -> int:
    """
    Converts a size expressed as a string with digits an unit (like `"5MB"`) to an integer (in bytes).

    Args:
        size (`int` or `str`): The size to convert. Will be directly returned if an `int`.

    Example:

    ```py
    >>> convert_file_size_to_int("1MiB")
    1048576
    ```
    """
    if isinstance(size, int):
        return size
    if size.upper().endswith("GIB"):
        return int(size[:-3]) * (2**30)
    if size.upper().endswith("MIB"):
        return int(size[:-3]) * (2**20)
    if size.upper().endswith("KIB"):
        return int(size[:-3]) * (2**10)
    if size.upper().endswith("GB"):
        int_size = int(size[:-2]) * (10**9)
        return int_size // 8 if size.endswith("b") else int_size
    if size.upper().endswith("MB"):
        int_size = int(size[:-2]) * (10**6)
        return int_size // 8 if size.endswith("b") else int_size
    if size.upper().endswith("KB"):
        int_size = int(size[:-2]) * (10**3)
        return int_size // 8 if size.endswith("b") else int_size
    raise ValueError("`size` is not in a valid format. Use an integer followed by the unit, e.g., '5GB'.")


def string_to_dict(string: str, pattern: str) -> Dict[str, str]:
    """Un-format a string using a python f-string pattern.
    From https://stackoverflow.com/a/36838374

    Example::

        >>> p = 'hello, my name is {name} and I am a {age} year old {what}'
        >>> s = p.format(name='cody', age=18, what='quarterback')
        >>> s
        'hello, my name is cody and I am a 18 year old quarterback'
        >>> string_to_dict(s, p)
        {'age': '18', 'name': 'cody', 'what': 'quarterback'}

    Args:
        string (str): input string
        pattern (str): pattern formatted like a python f-string

    Returns:
        Dict[str, str]: dictionary of variable -> value, retrieved from the input using the pattern

    Raises:
        ValueError: if the string doesn't match the pattern
    """
    regex = re.sub(r"{(.+?)}", r"(?P<_\1>.+)", pattern)
    result = re.search(regex, string)
    if result is None:
        raise ValueError(f"String {string} doesn't match the pattern {pattern}")
    values = list(result.groups())
    keys = re.findall(r"{(.+?)}", pattern)
    _dict = dict(zip(keys, values))
    return _dict


def asdict(obj):
    """Convert an object to its dictionary representation recursively.

    <Added version="2.4.0"/>
    """

    # Implementation based on https://docs.python.org/3/library/dataclasses.html#dataclasses.asdict

    def _is_dataclass_instance(obj):
        # https://docs.python.org/3/library/dataclasses.html#dataclasses.is_dataclass
        return is_dataclass(obj) and not isinstance(obj, type)

    def _asdict_inner(obj):
        if _is_dataclass_instance(obj):
            result = {}
            for f in fields(obj):
                value = _asdict_inner(getattr(obj, f.name))
                result[f.name] = value
            return result
        elif isinstance(obj, tuple) and hasattr(obj, "_fields"):
            # obj is a namedtuple
            return type(obj)(*[_asdict_inner(v) for v in obj])
        elif isinstance(obj, (list, tuple)):
            # Assume we can create an object of this type by passing in a
            # generator (which is not true for namedtuples, handled
            # above).
            return type(obj)(_asdict_inner(v) for v in obj)
        elif isinstance(obj, dict):
            return {_asdict_inner(k): _asdict_inner(v) for k, v in obj.items()}
        else:
            return copy.deepcopy(obj)

    if not isinstance(obj, dict) and not _is_dataclass_instance(obj):
        raise TypeError(f"{obj} is not a dict or a dataclass")

    return _asdict_inner(obj)


@contextlib.contextmanager
def temporary_assignment(obj, attr, value):
    """Temporarily assign obj.attr to value."""
    original = getattr(obj, attr, None)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, original)


@contextmanager
def temp_seed(seed: int, set_pytorch=False, set_tensorflow=False):
    """Temporarily set the random seed. This works for python numpy, pytorch and tensorflow."""
    np_state = np.random.get_state()
    np.random.seed(seed)

    if set_pytorch and config.TORCH_AVAILABLE:
        import torch

        torch_state = torch.random.get_rng_state()
        torch.random.manual_seed(seed)

        if torch.cuda.is_available():
            torch_cuda_states = torch.cuda.get_rng_state_all()
            torch.cuda.manual_seed_all(seed)

    if set_tensorflow and config.TF_AVAILABLE:
        import tensorflow as tf
        from tensorflow.python import context as tfpycontext

        tf_state = tf.random.get_global_generator()
        temp_gen = tf.random.Generator.from_seed(seed)
        tf.random.set_global_generator(temp_gen)

        if not tf.executing_eagerly():
            raise ValueError("Setting random seed for TensorFlow is only available in eager mode")

        tf_context = tfpycontext.context()  # eager mode context
        tf_seed = tf_context._seed
        tf_rng_initialized = hasattr(tf_context, "_rng")
        if tf_rng_initialized:
            tf_rng = tf_context._rng
        tf_context._set_global_seed(seed)

    try:
        yield
    finally:
        np.random.set_state(np_state)

        if set_pytorch and config.TORCH_AVAILABLE:
            torch.random.set_rng_state(torch_state)
            if torch.cuda.is_available():
                torch.cuda.set_rng_state_all(torch_cuda_states)

        if set_tensorflow and config.TF_AVAILABLE:
            tf.random.set_global_generator(tf_state)

            tf_context._seed = tf_seed
            if tf_rng_initialized:
                tf_context._rng = tf_rng
            else:
                delattr(tf_context, "_rng")


def unique_values(values):
    """Iterate over iterable and return only unique values in order."""
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            yield value


def no_op_if_value_is_null(func):
    """If the value is None, return None, else call `func`."""

    def wrapper(value):
        return func(value) if value is not None else None

    return wrapper


def first_non_null_value(iterable):
    """Return the index and the value of the first non-null value in the iterable. If all values are None, return -1 as index."""
    for i, value in enumerate(iterable):
        if value is not None:
            return i, value
    return -1, None


def zip_dict(*dicts):
    """Iterate over items of dictionaries grouped by their keys."""
    for key in unique_values(itertools.chain(*dicts)):  # set merge all keys
        # Will raise KeyError if the dict don't have the same keys
        yield key, tuple(d[key] for d in dicts)


class NonMutableDict(dict):
    """Dict where keys can only be added but not modified.

    Will raise an error if the user try to overwrite one key. The error message
    can be customized during construction. It will be formatted using {key} for
    the overwritten key.
    """

    def __init__(self, *args, **kwargs):
        self._error_msg = kwargs.pop(
            "error_msg",
            "Try to overwrite existing key: {key}",
        )
        if kwargs:
            raise ValueError("NonMutableDict cannot be initialized with kwargs.")
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        if key in self:
            raise ValueError(self._error_msg.format(key=key))
        return super().__setitem__(key, value)

    def update(self, other):
        if any(k in self for k in other):
            raise ValueError(self._error_msg.format(key=set(self) & set(other)))
        return super().update(other)


class classproperty(property):  # pylint: disable=invalid-name
    """Descriptor to be used as decorator for @classmethods."""

    def __get__(self, obj, objtype=None):
        return self.fget.__get__(None, objtype)()


def _single_map_nested(args):
    """Apply a function recursively to each element of a nested data struct."""
    function, data_struct, types, rank, disable_tqdm, desc = args

    # Singleton first to spare some computation
    if not isinstance(data_struct, dict) and not isinstance(data_struct, types):
        return function(data_struct)

    # Reduce logging to keep things readable in multiprocessing with tqdm
    if rank is not None and logging.get_verbosity() < logging.WARNING:
        logging.set_verbosity_warning()
    # Print at least one thing to fix tqdm in notebooks in multiprocessing
    # see https://github.com/tqdm/tqdm/issues/485#issuecomment-473338308
    if rank is not None and not disable_tqdm and any("notebook" in tqdm_cls.__name__ for tqdm_cls in tqdm.__mro__):
        print(" ", end="", flush=True)

    # Loop over single examples or batches and write to buffer/file if examples are to be updated
    pbar_iterable = data_struct.items() if isinstance(data_struct, dict) else data_struct
    pbar_desc = (desc + " " if desc is not None else "") + "#" + str(rank) if rank is not None else desc
    pbar = logging.tqdm(pbar_iterable, disable=disable_tqdm, position=rank, unit="obj", desc=pbar_desc)

    if isinstance(data_struct, dict):
        return {k: _single_map_nested((function, v, types, None, True, None)) for k, v in pbar}
    else:
        mapped = [_single_map_nested((function, v, types, None, True, None)) for v in pbar]
        if isinstance(data_struct, list):
            return mapped
        elif isinstance(data_struct, tuple):
            return tuple(mapped)
        else:
            return np.array(mapped)


def map_nested(
    function: Callable[[Any], Any],
    data_struct: Any,
    dict_only: bool = False,
    map_list: bool = True,
    map_tuple: bool = False,
    map_numpy: bool = False,
    num_proc: Optional[int] = None,
    parallel_min_length: int = 2,
    types: Optional[tuple] = None,
    disable_tqdm: bool = True,
    desc: Optional[str] = None,
) -> Any:
    """Apply a function recursively to each element of a nested data struct.

    Use multiprocessing if num_proc > 1 and the length of data_struct is greater than or equal to
    `parallel_min_length`.

    <Changed version="2.5.0">

    Before version 2.5.0, multiprocessing was not used if `num_proc` was greater than or equal to ``len(iterable)``.

    Now, if `num_proc` is greater than or equal to ``len(iterable)``, `num_proc` is set to ``len(iterable)`` and
    multiprocessing is used.

    </Changed>

    Args:
        function (`Callable`): Function to be applied to `data_struct`.
        data_struct (`Any`): Data structure to apply `function` to.
        dict_only (`bool`, default `False`): Whether only apply `function` recursively to `dict` values in
            `data_struct`.
        map_list (`bool`, default `True`): Whether also apply `function` recursively to `list` elements (besides `dict`
            values).
        map_tuple (`bool`, default `False`): Whether also apply `function` recursively to `tuple` elements (besides
            `dict` values).
        map_numpy (`bool, default `False`): Whether also apply `function` recursively to `numpy.array` elements (besides
            `dict` values).
        num_proc (`int`, *optional*): Number of processes.
        parallel_min_length (`int`, default `2`): Minimum length of `data_struct` required for parallel
            processing.
            <Added version="2.5.0"/>
        types (`tuple`, *optional*): Additional types (besides `dict` values) to apply `function` recursively to their
            elements.
        disable_tqdm (`bool`, default `True`): Whether to disable the tqdm progressbar.
        desc (`str`, *optional*): Prefix for the tqdm progressbar.

    Returns:
        `Any`
    """
    if types is None:
        types = []
        if not dict_only:
            if map_list:
                types.append(list)
            if map_tuple:
                types.append(tuple)
            if map_numpy:
                types.append(np.ndarray)
        types = tuple(types)

    # Singleton
    if not isinstance(data_struct, dict) and not isinstance(data_struct, types):
        return function(data_struct)

    disable_tqdm = disable_tqdm or not logging.is_progress_bar_enabled()
    iterable = list(data_struct.values()) if isinstance(data_struct, dict) else data_struct

    if num_proc is None:
        num_proc = 1
    if num_proc <= 1 or len(iterable) < parallel_min_length:
        mapped = [
            _single_map_nested((function, obj, types, None, True, None))
            for obj in logging.tqdm(iterable, disable=disable_tqdm, desc=desc)
        ]
    else:
        num_proc = num_proc if num_proc <= len(iterable) else len(iterable)
        split_kwds = []  # We organize the splits ourselve (contiguous splits)
        for index in range(num_proc):
            div = len(iterable) // num_proc
            mod = len(iterable) % num_proc
            start = div * index + min(index, mod)
            end = start + div + (1 if index < mod else 0)
            split_kwds.append((function, iterable[start:end], types, index, disable_tqdm, desc))

        if len(iterable) != sum(len(i[1]) for i in split_kwds):
            raise ValueError(
                f"Error dividing inputs iterable among processes. "
                f"Total number of objects {len(iterable)}, "
                f"length: {sum(len(i[1]) for i in split_kwds)}"
            )

        logger.info(
            f"Spawning {num_proc} processes for {len(iterable)} objects in slices of {[len(i[1]) for i in split_kwds]}"
        )
        initargs, initializer = None, None
        if not disable_tqdm:
            initargs, initializer = (RLock(),), tqdm.set_lock
        with Pool(num_proc, initargs=initargs, initializer=initializer) as pool:
            mapped = pool.map(_single_map_nested, split_kwds)
        logger.info(f"Finished {num_proc} processes")
        mapped = [obj for proc_res in mapped for obj in proc_res]
        logger.info(f"Unpacked {len(mapped)} objects")

    if isinstance(data_struct, dict):
        return dict(zip(data_struct.keys(), mapped))
    else:
        if isinstance(data_struct, list):
            return mapped
        elif isinstance(data_struct, tuple):
            return tuple(mapped)
        else:
            return np.array(mapped)


class NestedDataStructure:
    def __init__(self, data=None):
        self.data = data if data is not None else []

    def flatten(self, data=None):
        data = data if data is not None else self.data
        if isinstance(data, dict):
            return self.flatten(list(data.values()))
        elif isinstance(data, (list, tuple)):
            return [flattened for item in data for flattened in self.flatten(item)]
        else:
            return [data]


def has_sufficient_disk_space(needed_bytes, directory="."):
    try:
        free_bytes = disk_usage(os.path.abspath(directory)).free
    except OSError:
        return True
    return needed_bytes < free_bytes


def _convert_github_url(url_path: str) -> Tuple[str, Optional[str]]:
    """Convert a link to a file on a github repo in a link to the raw github object."""
    parsed = urlparse(url_path)
    sub_directory = None
    if parsed.scheme in ("http", "https", "s3") and parsed.netloc == "github.com":
        if "blob" in url_path:
            if not url_path.endswith(".py"):
                raise ValueError(f"External import from github at {url_path} should point to a file ending with '.py'")
            url_path = url_path.replace("blob", "raw")  # Point to the raw file
        else:
            # Parse github url to point to zip
            github_path = parsed.path[1:]
            repo_info, branch = github_path.split("/tree/") if "/tree/" in github_path else (github_path, "master")
            repo_owner, repo_name = repo_info.split("/")
            url_path = f"https://github.com/{repo_owner}/{repo_name}/archive/{branch}.zip"
            sub_directory = f"{repo_name}-{branch}"
    return url_path, sub_directory


def get_imports(file_path: str) -> Tuple[str, str, str, str]:
    """Find whether we should import or clone additional files for a given processing script.
        And list the import.

    We allow:
    - library dependencies,
    - local dependencies and
    - external dependencies whose url is specified with a comment starting from "# From:' followed by the raw url to a file, an archive or a github repository.
        external dependencies will be downloaded (and extracted if needed in the dataset folder).
        We also add an `__init__.py` to each sub-folder of a downloaded folder so the user can import from them in the script.

    Note that only direct import in the dataset processing script will be handled
    We don't recursively explore the additional import to download further files.

    Example::

        import tensorflow
        import .c4_utils
        import .clicr.dataset-code.build_json_dataset  # From: https://raw.githubusercontent.com/clips/clicr/master/dataset-code/build_json_dataset
    """
    lines = []
    with open(file_path, encoding="utf-8") as f:
        lines.extend(f.readlines())

    logger.debug(f"Checking {file_path} for additional imports.")
    imports: List[Tuple[str, str, str, Optional[str]]] = []
    is_in_docstring = False
    for line in lines:
        docstr_start_match = re.findall(r'[\s\S]*?"""[\s\S]*?', line)

        if len(docstr_start_match) == 1:
            # flip True <=> False only if doctstring
            # starts at line without finishing
            is_in_docstring = not is_in_docstring

        if is_in_docstring:
            # import statements in doctstrings should
            # not be added as required dependencies
            continue

        match = re.match(r"^import\s+(\.?)([^\s\.]+)[^#\r\n]*(?:#\s+From:\s+)?([^\r\n]*)", line, flags=re.MULTILINE)
        if match is None:
            match = re.match(
                r"^from\s+(\.?)([^\s\.]+)(?:[^\s]*)\s+import\s+[^#\r\n]*(?:#\s+From:\s+)?([^\r\n]*)",
                line,
                flags=re.MULTILINE,
            )
            if match is None:
                continue
        if match.group(1):
            # The import starts with a '.', we will download the relevant file
            if any(imp[1] == match.group(2) for imp in imports):
                # We already have this import
                continue
            if match.group(3):
                # The import has a comment with 'From:', we'll retrieve it from the given url
                url_path = match.group(3)
                url_path, sub_directory = _convert_github_url(url_path)
                imports.append(("external", match.group(2), url_path, sub_directory))
            elif match.group(2):
                # The import should be at the same place as the file
                imports.append(("internal", match.group(2), match.group(2), None))
        else:
            if match.group(3):
                # The import has a comment with `From: git+https:...`, asks user to pip install from git.
                url_path = match.group(3)
                imports.append(("library", match.group(2), url_path, None))
            else:
                imports.append(("library", match.group(2), match.group(2), None))

    return imports


class Pickler(dill.Pickler):
    """Same Pickler as the one from dill, but improved for notebooks and shells"""

    dispatch = dill._dill.MetaCatchingDict(dill.Pickler.dispatch.copy())

    def memoize(self, obj):
        # don't memoize strings since two identical strings can have different python ids
        if type(obj) != str:
            dill.Pickler.memoize(self, obj)


def dump(obj, file):
    """pickle an object to a file"""
    Pickler(file, recurse=True).dump(obj)
    return


@contextlib.contextmanager
def _no_cache_fields(obj):
    try:
        if (
            "PreTrainedTokenizerBase" in [base_class.__name__ for base_class in type(obj).__mro__]
            and hasattr(obj, "cache")
            and isinstance(obj.cache, dict)
        ):
            with temporary_assignment(obj, "cache", {}):
                yield
        else:
            yield

    except ImportError:
        yield


def dumps(obj):
    """pickle an object to a string"""
    file = StringIO()
    with _no_cache_fields(obj):
        dump(obj, file)
    return file.getvalue()


def pklregister(t):
    def proxy(func):
        Pickler.dispatch[t] = func
        return func

    return proxy


@pklregister(CodeType)
def _save_code(pickler, obj):
    """
    From dill._dill.save_code
    This is a modified version that removes the origin (filename + line no.)
    of functions created in notebooks or shells for example.
    """
    dill._dill.log.info(f"Co: {obj}")
    # The filename of a function is the .py file where it is defined.
    # Filenames of functions created in notebooks or shells start with '<'
    # ex: <ipython-input-13-9ed2afe61d25> for ipython, and <stdin> for shell
    # Moreover lambda functions have a special name: '<lambda>'
    # ex: (lambda x: x).__code__.co_name == "<lambda>"  # True
    #
    # For the hashing mechanism we ignore where the function has been defined
    # More specifically:
    # - we ignore the filename of special functions (filename starts with '<')
    # - we always ignore the line number
    # - we only use the base name of the file instead of the whole path,
    # to be robust in case a script is moved for example.
    #
    # Only those two lines are different from the original implementation:
    co_filename = (
        "" if obj.co_filename.startswith("<") or obj.co_name == "<lambda>" else os.path.basename(obj.co_filename)
    )
    co_firstlineno = 1
    # The rest is the same as in the original dill implementation
    if dill._dill.PY3:
        if hasattr(obj, "co_posonlyargcount"):
            args = (
                obj.co_argcount,
                obj.co_posonlyargcount,
                obj.co_kwonlyargcount,
                obj.co_nlocals,
                obj.co_stacksize,
                obj.co_flags,
                obj.co_code,
                obj.co_consts,
                obj.co_names,
                obj.co_varnames,
                co_filename,
                obj.co_name,
                co_firstlineno,
                obj.co_lnotab,
                obj.co_freevars,
                obj.co_cellvars,
            )
        else:
            args = (
                obj.co_argcount,
                obj.co_kwonlyargcount,
                obj.co_nlocals,
                obj.co_stacksize,
                obj.co_flags,
                obj.co_code,
                obj.co_consts,
                obj.co_names,
                obj.co_varnames,
                co_filename,
                obj.co_name,
                co_firstlineno,
                obj.co_lnotab,
                obj.co_freevars,
                obj.co_cellvars,
            )
    else:
        args = (
            obj.co_argcount,
            obj.co_nlocals,
            obj.co_stacksize,
            obj.co_flags,
            obj.co_code,
            obj.co_consts,
            obj.co_names,
            obj.co_varnames,
            co_filename,
            obj.co_name,
            co_firstlineno,
            obj.co_lnotab,
            obj.co_freevars,
            obj.co_cellvars,
        )
    pickler.save_reduce(CodeType, args, obj=obj)
    dill._dill.log.info("# Co")
    return


if config.DILL_VERSION < version.parse("0.3.5"):

    @pklregister(FunctionType)
    def save_function(pickler, obj):
        """
        From dill._dill.save_function
        This is a modified version that make globs deterministic since the order of
        the keys in the output dictionary of globalvars can change.
        """
        if not dill._dill._locate_function(obj):
            dill._dill.log.info(f"F1: {obj}")
            if getattr(pickler, "_recurse", False):
                # recurse to get all globals referred to by obj
                globalvars = dill.detect.globalvars
                globs = globalvars(obj, recurse=True, builtin=True)
                if id(obj) in dill._dill.stack:
                    globs = obj.__globals__ if dill._dill.PY3 else obj.func_globals
            else:
                globs = obj.__globals__ if dill._dill.PY3 else obj.func_globals
            # globs is a dictionary with keys = var names (str) and values = python objects
            # however the dictionary is not always loaded in the same order
            # therefore we have to sort the keys to make deterministic.
            # This is important to make `dump` deterministic.
            # Only this line is different from the original implementation:
            globs = dict(sorted(globs.items()))
            # The rest is the same as in the original dill implementation
            _byref = getattr(pickler, "_byref", None)
            _recurse = getattr(pickler, "_recurse", None)
            _memo = (id(obj) in dill._dill.stack) and (_recurse is not None)
            dill._dill.stack[id(obj)] = len(dill._dill.stack), obj
            if dill._dill.PY3:
                _super = ("super" in getattr(obj.__code__, "co_names", ())) and (_byref is not None)
                if _super:
                    pickler._byref = True
                if _memo:
                    pickler._recurse = False
                fkwdefaults = getattr(obj, "__kwdefaults__", None)
                pickler.save_reduce(
                    dill._dill._create_function,
                    (obj.__code__, globs, obj.__name__, obj.__defaults__, obj.__closure__, obj.__dict__, fkwdefaults),
                    obj=obj,
                )
            else:
                _super = (
                    ("super" in getattr(obj.func_code, "co_names", ()))
                    and (_byref is not None)
                    and getattr(pickler, "_recurse", False)
                )
                if _super:
                    pickler._byref = True
                if _memo:
                    pickler._recurse = False
                pickler.save_reduce(
                    dill._dill._create_function,
                    (obj.func_code, globs, obj.func_name, obj.func_defaults, obj.func_closure, obj.__dict__),
                    obj=obj,
                )
            if _super:
                pickler._byref = _byref
            if _memo:
                pickler._recurse = _recurse
            if (
                dill._dill.OLDER
                and not _byref
                and (_super or (not _super and _memo) or (not _super and not _memo and _recurse))
            ):
                pickler.clear_memo()
            dill._dill.log.info("# F1")
        else:
            dill._dill.log.info(f"F2: {obj}")
            name = getattr(obj, "__qualname__", getattr(obj, "__name__", None))
            dill._dill.StockPickler.save_global(pickler, obj, name=name)
            dill._dill.log.info("# F2")
        return

else:  # config.DILL_VERSION >= version.parse("0.3.5")

    # https://github.com/uqfoundation/dill/blob/dill-0.3.5.1/dill/_dill.py
    @pklregister(FunctionType)
    def save_function(pickler, obj):
        if not dill._dill._locate_function(obj, pickler):
            dill._dill.log.info("F1: %s" % obj)
            _recurse = getattr(pickler, "_recurse", None)
            # _byref = getattr(pickler, "_byref", None)  # TODO: not used
            _postproc = getattr(pickler, "_postproc", None)
            _main_modified = getattr(pickler, "_main_modified", None)
            _original_main = getattr(pickler, "_original_main", dill._dill.__builtin__)  # 'None'
            postproc_list = []
            if _recurse:
                # recurse to get all globals referred to by obj
                from dill.detect import globalvars

                globs_copy = globalvars(obj, recurse=True, builtin=True)

                # Add the name of the module to the globs dictionary to prevent
                # the duplication of the dictionary. Pickle the unpopulated
                # globals dictionary and set the remaining items after the function
                # is created to correctly handle recursion.
                globs = {"__name__": obj.__module__}
            else:
                globs_copy = obj.__globals__ if dill._dill.PY3 else obj.func_globals

                # If the globals is the __dict__ from the module being saved as a
                # session, substitute it by the dictionary being actually saved.
                if _main_modified and globs_copy is _original_main.__dict__:
                    globs_copy = getattr(pickler, "_main", _original_main).__dict__
                    globs = globs_copy
                # If the globals is a module __dict__, do not save it in the pickle.
                elif (
                    globs_copy is not None
                    and obj.__module__ is not None
                    and getattr(dill._dill._import_module(obj.__module__, True), "__dict__", None) is globs_copy
                ):
                    globs = globs_copy
                else:
                    globs = {"__name__": obj.__module__}

            # DONE: modified here for huggingface/datasets
            # - globs is a dictionary with keys = var names (str) and values = python objects
            # - globs_copy is a dictionary with keys = var names (str) and values = ids of the python objects
            # however the dictionary is not always loaded in the same order
            # therefore we have to sort the keys to make deterministic.
            # This is important to make `dump` deterministic.
            # Only these line are different from the original implementation:
            # START
            globs_is_globs_copy = globs is globs_copy
            globs = dict(sorted(globs.items()))
            if globs_is_globs_copy:
                globs_copy = globs
            elif globs_copy is not None:
                globs_copy = dict(sorted(globs_copy.items()))
            # END

            if globs_copy is not None and globs is not globs_copy:
                # In the case that the globals are copied, we need to ensure that
                # the globals dictionary is updated when all objects in the
                # dictionary are already created.
                if dill._dill.PY3:
                    glob_ids = {id(g) for g in globs_copy.values()}
                else:
                    glob_ids = {id(g) for g in globs_copy.itervalues()}
                for stack_element in _postproc:
                    if stack_element in glob_ids:
                        _postproc[stack_element].append((dill._dill._setitems, (globs, globs_copy)))
                        break
                else:
                    postproc_list.append((dill._dill._setitems, (globs, globs_copy)))

            if dill._dill.PY3:
                closure = obj.__closure__
                state_dict = {}
                for fattrname in ("__doc__", "__kwdefaults__", "__annotations__"):
                    fattr = getattr(obj, fattrname, None)
                    if fattr is not None:
                        state_dict[fattrname] = fattr
                if obj.__qualname__ != obj.__name__:
                    state_dict["__qualname__"] = obj.__qualname__
                if "__name__" not in globs or obj.__module__ != globs["__name__"]:
                    state_dict["__module__"] = obj.__module__

                state = obj.__dict__
                if type(state) is not dict:
                    state_dict["__dict__"] = state
                    state = None
                if state_dict:
                    state = state, state_dict

                dill._dill._save_with_postproc(
                    pickler,
                    (
                        dill._dill._create_function,
                        (obj.__code__, globs, obj.__name__, obj.__defaults__, closure),
                        state,
                    ),
                    obj=obj,
                    postproc_list=postproc_list,
                )
            else:
                closure = obj.func_closure
                if obj.__doc__ is not None:
                    postproc_list.append((setattr, (obj, "__doc__", obj.__doc__)))
                if "__name__" not in globs or obj.__module__ != globs["__name__"]:
                    postproc_list.append((setattr, (obj, "__module__", obj.__module__)))
                if obj.__dict__:
                    postproc_list.append((setattr, (obj, "__dict__", obj.__dict__)))

                dill._dill._save_with_postproc(
                    pickler,
                    (dill._dill._create_function, (obj.func_code, globs, obj.func_name, obj.func_defaults, closure)),
                    obj=obj,
                    postproc_list=postproc_list,
                )

            # Lift closure cell update to earliest function (#458)
            if _postproc:
                topmost_postproc = next(iter(_postproc.values()), None)
                if closure and topmost_postproc:
                    for cell in closure:
                        possible_postproc = (setattr, (cell, "cell_contents", obj))
                        try:
                            topmost_postproc.remove(possible_postproc)
                        except ValueError:
                            continue

                        # Change the value of the cell
                        pickler.save_reduce(*possible_postproc)
                        # pop None created by calling preprocessing step off stack
                        if dill._dill.PY3:
                            pickler.write(bytes("0", "UTF-8"))
                        else:
                            pickler.write("0")

            dill._dill.log.info("# F1")
        else:
            dill._dill.log.info("F2: %s" % obj)
            name = getattr(obj, "__qualname__", getattr(obj, "__name__", None))
            dill._dill.StockPickler.save_global(pickler, obj, name=name)
            dill._dill.log.info("# F2")
        return


def copyfunc(func):
    result = types.FunctionType(func.__code__, func.__globals__, func.__name__, func.__defaults__, func.__closure__)
    result.__kwdefaults__ = func.__kwdefaults__
    return result


try:
    import regex

    @pklregister(type(regex.Regex("", 0)))
    def _save_regex(pickler, obj):
        dill._dill.log.info(f"Re: {obj}")
        args = (
            obj.pattern,
            obj.flags,
        )
        pickler.save_reduce(regex.compile, args, obj=obj)
        dill._dill.log.info("# Re")
        return

except ImportError:
    pass
