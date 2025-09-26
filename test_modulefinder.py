import collections.abc as _cabc
import importlib.machinery
import os
import py_compile
import sysconfig
from pathlib import Path
from textwrap import dedent

import pytest

import modulefinder_revamped as modulefinder


NestedMapping = _cabc.Mapping[str, "NestedMapping | str | bytes"]


STDLIB_PATH = sysconfig.get_path("stdlib")


def create_file_tree(path: Path, dir_contents: NestedMapping) -> None:
    """Create a tree of files based on a (nested) dict of file/directory names and (source) contents.

    Warning: Be careful when using escape sequences in file contents strings. Consider escaping them or using raw
    strings.
    """

    for filename, value in dir_contents.items():
        filepath = path / filename
        if isinstance(value, dict):
            filepath.mkdir()
            create_file_tree(filepath, value)
        elif isinstance(value, str):
            filepath.write_text(value, encoding="utf-8")
        elif isinstance(value, bytes):
            filepath.write_bytes(value)
        else:  # pragma: no cover
            msg = f"Expected a dict, string, or bytes object, got {value!r}."
            raise TypeError(msg)


def run_module_finder(  # noqa: PLR0913
    test_path: list[str],
    import_this: str,
    expected_modules: set[str],
    expected_missing: list[str],
    expected_maybe_missing: list[str],
    path_replacements: list[tuple[str, str]] | None = None,
) -> None:
    mf = modulefinder.ModuleFinder(test_path, path_replacements)
    mf.import_as_module(import_this)

    # Check if we found what we expected, not more, not less.
    assert set(mf.modules) == expected_modules

    # Check for missing and maybe missing modules.
    missing, maybe_missing = mf.any_missing_maybe()
    assert missing == expected_missing
    assert maybe_missing == expected_maybe_missing


# Each test description is a list of 5 items:
#
# 1. a dictionary specifying a file tree to create; the format is obvious imo.
# 2. a module name that will be imported by modulefinder
# 3. a set of module names that modulefinder is required to find
# 4. a list of module names that modulefinder should complain
#    about because they are not found
# 5. a list of module names that modulefinder should complain
#    about because they MAY be not found
#
# Modulefinder searches in a path that contains temp_path, plus
# the standard Lib directory.


@pytest.mark.parametrize(
    ("source", "import_this", "expected_modules", "expected_missing", "expected_maybe_missing"),
    [
        pytest.param(
            {
                "mymodule.py": "",
                "a": {
                    "__init__.py": dedent("""\
                        import blahblah
                        from a import b
                        import c
                    """),
                    "module.py": dedent("""\
                        import sys
                        from a import b as x
                        from a.c import sillyname
                    """),
                    "b.py": "",
                    "c.py": dedent("""\
                        from a.module import x
                        import mymodule as sillyname
                        from sys import version_info
                    """),
                },
            },
            "a.module",
            {"a", "a.b", "a.c", "a.module", "mymodule", "sys"},
            ["blahblah", "c"],
            [],
            id="package",
        ),
        pytest.param(
            {
                "a": {
                    "__init__.py": "",
                    "module.py": dedent("""\
                        from b import something
                        from c import something
                    """),
                },
                "b": {
                    "__init__.py": dedent("""\
                        from sys import *
                    """)
                },
            },
            "a.module",
            {"a", "a.module", "sys", "b"},
            ["c"],
            ["b.something"],
            id="maybe",
        ),
        pytest.param(
            {
                "a": {
                    "__init__.py": "",
                    "module.py": dedent("""\
                        from b import something
                        from c import something
                    """),
                },
                "b": {
                    "__init__.py": dedent("""\
                        from __future__ import absolute_import
                        from sys import *
                    """)
                },
            },
            "a.module",
            {"a", "a.module", "sys", "b", "__future__"},
            ["c"],
            ["b.something"],
            id="maybe-new",
        ),
        pytest.param(
            {
                "mymodule.py": "",
                "a": {
                    "__init__.py": "",
                    "module.py": dedent("""\
                        from __future__ import absolute_import
                        import sys # sys
                        import blahblah # fails
                        import gc # gc
                        import b.x # b.x
                        from b import y # b.y
                        from b.z import * # b.z.*
                    """),
                    "gc.py": "",
                    "sys.py": "import mymodule",
                    "b": {
                        "__init__.py": "",
                        "x.py": "",
                        "y.py": "",
                        "z.py": "",
                    },
                },
                "b": {
                    "__init__.py": "import z",
                    "unused.py": "",
                    "x.py": "",
                    "y.py": "",
                    "z.py": "",
                },
            },
            "a.module",
            {"a", "a.module", "b", "b.x", "b.y", "b.z", "__future__", "sys", "gc"},
            ["blahblah", "z"],
            [],
            id="absolute-imports",
        ),
        pytest.param(
            {
                "mymodule.py": "",
                "a": {
                    "__init__.py": "from .b import y, z # a.b.y, a.b.z",
                    "module.py": dedent("""\
                        from __future__ import absolute_import # __future__
                        import gc # gc
                    """),
                    "gc.py": "",
                    "sys.py": "",
                    "b": {
                        "__init__.py": dedent("""\
                            from ..b import x # a.b.x
                            #from a.b.c import moduleC
                            from .c import moduleC # a.b.moduleC
                        """),
                        "x.py": "",
                        "y.py": "",
                        "z.py": "",
                        "g.py": "",
                        "c": {
                            "__init__.py": "from ..c import e # a.b.c.e",
                            "moduleC.py": "from ..c import d # a.b.c.d",
                            "d.py": "",
                            "e.py": "",
                            "x.py": "",
                        },
                    },
                },
            },
            "a.module",
            {
                "__future__",
                "a",
                "a.module",
                "a.b",
                "a.b.y",
                "a.b.z",
                "a.b.c",
                "a.b.c.moduleC",
                "a.b.c.d",
                "a.b.c.e",
                "a.b.x",
                "gc",
            },
            [],
            [],
            id="relative-imports",
        ),
        pytest.param(
            {
                "mymodule.py": "",
                "a": {
                    "__init__.py": "from . import sys # a.sys",
                    "another.py": "",
                    "module.py": "from .b import y, z # a.b.y, a.b.z",
                    "gc.py": "",
                    "sys.py": "",
                    "b": {
                        "__init__.py": dedent("""\
                            from .c import moduleC # a.b.c.moduleC
                            from .c import d # a.b.c.d
                        """),
                        "x.py": "",
                        "y.py": "",
                        "z.py": "",
                        "c": {
                            "__init__.py": "from . import e # a.b.c.e",
                            "moduleC.py": dedent("""\
                                #
                                from . import f   # a.b.c.f
                                from .. import x  # a.b.x
                                from ... import another # a.another
                            """),
                            "d.py": "",
                            "e.py": "",
                            "f.py": "",
                        },
                    },
                },
            },
            "a.module",
            {
                "a",
                "a.module",
                "a.sys",
                "a.b",
                "a.b.y",
                "a.b.z",
                "a.b.c",
                "a.b.c.d",
                "a.b.c.e",
                "a.b.c.moduleC",
                "a.b.c.f",
                "a.b.x",
                "a.another",
            },
            [],
            [],
            id="relative-imports-2",
        ),
        pytest.param(
            {
                "a": {
                    "__init__.py": "def foo(): pass",
                    "module.py": dedent("""\
                        from . import foo
                        from . import bar
                    """),
                }
            },
            "a.module",
            {"a", "a.module"},
            ["a.bar"],
            [],
            id="relative-imports-3",
        ),
        pytest.param(
            {
                "a": {
                    "__init__.py": "def foo(): pass",
                    "module.py": "from . import *",
                }
            },
            "a.module",
            {"a", "a.module"},
            [],
            [],
            id="relative-imports-4",
        ),
        pytest.param(
            {
                "a": {
                    "__init__.py": "",
                    "module.py": "import b.module",
                },
                "b": {
                    "__init__.py": "",
                    "module.py": "?  # SyntaxError: invalid syntax",
                },
            },
            "a.module",
            {"a", "a.module", "b"},
            ["b.module"],
            [],
            id="syntax-error",
        ),
        pytest.param(
            {
                "a": {
                    "__init__.py": "",
                    "module.py": dedent("""\
                        import c
                        from b import c
                    """),
                },
                "b": {
                    "__init__.py": "",
                    "c.py": "",
                },
            },
            "a.module",
            {"a", "a.module", "b", "b.c"},
            ["c"],
            [],
            id="same-name-as-bad",
        ),
        pytest.param(
            # 2**16 constants
            {
                "a.py": dedent(f"""\
                    {list(range(2**16))!r}
                    import b
                """),
                "b.py": "",
            },
            "a",
            {"a", "b"},
            [],
            [],
            id="extended-opargs",
        ),
        pytest.param(
            {
                "a_utf8.py": dedent("""\
                    # use the default of utf8
                    print('Unicode test A code point 2090 \u2090 that is not valid in cp1252')
                    import b_utf8
                """),
                "b_utf8.py": dedent("""\
                    # use the default of utf8
                    print('Unicode test B code point 2090 \u2090 that is not valid in cp1252')
                """),
            },
            "a_utf8",
            {"a_utf8", "b_utf8"},
            [],
            [],
            id="encoding-utf8-default",
        ),
        pytest.param(
            {
                "a_utf8.py": dedent("""\
                    # coding=utf8
                    print('Unicode test A code point 2090 \u2090 that is not valid in cp1252')
                    import b_utf8
                """),
                "b_utf8.py": dedent("""\
                    # use the default of utf8
                    print('Unicode test B code point 2090 \u2090 that is not valid in cp1252')
                """),
            },
            "a_utf8",
            {"a_utf8", "b_utf8"},
            [],
            [],
            id="encoding-utf8-explicit",
        ),
        pytest.param(
            {
                "a_cp1252.py": b"\n".join(
                    (
                        b"# coding=cp1252",
                        b"# 0xe2 is not allowed in utf8",
                        b"print('CP1252 test P\xe2t\xe9')",
                        b"import b_utf8",
                    )
                ),
                "b_utf8.py": dedent("""\
                    # use the default of utf8
                    print('Unicode test A code point 2090 \u2090 that is not valid in cp1252')
                """),
            },
            "a_cp1252",
            {"a_cp1252", "b_utf8"},
            [],
            [],
            id="encoding-cp1252-explicit",
        ),
    ],
)
def test_e2e(  # noqa: PLR0913
    tmp_path: Path,
    source: NestedMapping,
    import_this: str,
    expected_modules: set[str],
    expected_missing: list[str],
    expected_maybe_missing: list[str],
):
    test_path = [os.fspath(tmp_path), STDLIB_PATH]
    create_file_tree(tmp_path, source)

    run_module_finder(test_path, import_this, expected_modules, expected_missing, expected_maybe_missing)


def test_e2e_bytecode(tmp_path: Path):
    test_path = [os.fspath(tmp_path), STDLIB_PATH]

    # Set up a bytecode file without an accompanying source file.
    base_path = tmp_path / "a"
    source_path = base_path.with_suffix(importlib.machinery.SOURCE_SUFFIXES[0])
    bytecode_path = base_path.with_suffix(importlib.machinery.BYTECODE_SUFFIXES[0])
    source_path.write_bytes(b"testing_modulefinder = True\n")
    py_compile.compile(os.fspath(source_path), cfile=os.fspath(bytecode_path))
    source_path.unlink()

    run_module_finder(test_path, "a", {"a"}, [], [])


def test_e2e_active_path_replacements(tmp_path: Path):
    test_path = [os.fspath(tmp_path), STDLIB_PATH]

    source_path = tmp_path / "hello.py"
    source_path.write_text(
        dedent("""\
            print('hello world')
            def hello():
                return "hello"
            
        """)
    )
    new_path = source_path.with_name("goodbye.py")

    path_replacements = [(os.fspath(source_path), os.fspath(new_path))]
    mf = modulefinder.ModuleFinder(test_path, path_replacements)
    mf.import_as_module("hello")

    hello_module = mf.modules["hello"]
    hello_code = hello_module.__code__

    assert hello_code is not None
    assert hello_code.co_filename == os.fspath(new_path)


def test_e2e_inactive_path_replacements(tmp_path: Path):
    test_path = [os.fspath(tmp_path), STDLIB_PATH]

    source_path = tmp_path / "hello.py"
    source_path.write_text(
        dedent("""\
            print('hello world')
            def hello():
                return "hello"
            
        """)
    )

    unrelated_old_path = source_path.with_name("hi.py")
    new_path = source_path.with_name("goodbye.py")

    path_replacements = [(os.fspath(unrelated_old_path), os.fspath(new_path))]
    mf = modulefinder.ModuleFinder(test_path, path_replacements)
    mf.import_as_module("hello")

    hello_module = mf.modules["hello"]
    hello_code = hello_module.__code__

    assert hello_code is not None
    assert hello_code.co_filename == os.fspath(source_path)
