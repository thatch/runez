import logging
import os
import re
import sys
from collections import defaultdict

from runez.program import is_executable, run
from runez.system import _R, abort, flattened, resolved_path, short, UNSET


CPYTHON_NAMES = ["python", "", "p", "py", "cpython"]
R_SPEC = re.compile(r"^\s*((|py?|c?python|(ana|mini)?conda[23]?|pypy)\s*[:-]?)\s*([0-9]*)\.?([0-9]*)\.?([0-9]*)\s*$", re.IGNORECASE)
R_VERSION = re.compile(r"^((\d+)((\.(\d+))*)((a|b|c|rc)(\d+))?(\.(dev|post|final)\.?(\d+))?).*$")
LOG = logging.getLogger(__name__)


def _is_path(text):
    if text:
        return text.startswith("~") or text.startswith(".") or "/" in text


def _simplified_python_path(path):
    """Simplify macos ridiculous paths"""
    if path and ".framework/" in path:
        location = "/usr/bin"
        if "Cellar" in path:
            i = path.index("Cellar")
            location = path[:i].rstrip("/")
            if not location.endswith("bin"):
                location = os.path.join(location, "bin")

        if "Versions/3" in path:
            return os.path.join(location, "python3")

        if "Versions/2" in path:
            return os.path.join(location, "python2")

    return path


class PrioritizedName(object):
    """Name with an assigned sorting priority"""

    def __init__(self, name, priority):
        self.name = name
        self.priority = priority

    def __repr__(self):
        return self.name

    def __lt__(self, other):
        if isinstance(other, PrioritizedName):
            return self.priority < other.priority

    def __eq__(self, other):
        return isinstance(other, PrioritizedName) and self.priority == other.priority


class OrderedByName(object):
    """Allows to order things arbitrarily by name"""

    def __init__(self, order=None, reverse=True, separator=","):
        order = flattened(order, split=separator, keep_empty=None)
        for name in self.__slots__:
            if name not in order:
                order.append(name)

        count = len(order)
        for i, name in enumerate(order):
            obj = PrioritizedName(name, count - i if reverse else i)
            setattr(self, name, obj)

    @property
    def effective_order(self):
        return sorted([getattr(self, n) for n in self.__slots__], reverse=True)

    def __repr__(self):
        return ",".join(str(s) for s in self.effective_order)


class Origins(OrderedByName):
    """
    Scanned python installations are sorted by where they came from, in this default order (highest priority first):
    - adhoc: python installations that were explicitly given by user, via full path to python exe
    - pyenv: pyenv-like installations (very quick to scan)
    - path: PATH-like env var (slower to scan), this
    """

    __slots__ = ["adhoc", "pyenv", "path"]


class Families(OrderedByName):
    """Allows to sort installations by python family"""

    __slots__ = ["cpython", "pypy", "conda"]

    @property
    def default_family(self):
        return self.cpython

    def guess_family(self, text):
        """Guessed python family from given 'text' (typically path to installation)"""
        if text:
            for name in self.__slots__:
                if name in text:
                    return getattr(self, name)

        return self.default_family


class PythonSpec(object):
    """
    Holds a canonical reference to a desired python installation
    Examples: 3, 3.9, py39, conda3.7.1, /usr/bin/python
    """

    def __init__(self, text, family):
        """
        Args:
            text (str): Text describing desired python
            family (PrioritizedName): Corresponding python family
        """
        text = text.strip() if text else ""
        self.family = family
        self.text = text
        self.version = None
        if text in CPYTHON_NAMES:
            self.canonical = "%s" % family
            return

        if _is_path(text):
            self.canonical = resolved_path(text)
            return

        self.canonical = "?%s" % text  # Don't let arbitrary given text accidentally count as valid canonical
        m = R_SPEC.match(text)
        if not m:
            return

        components = [s for s in (m.group(4), m.group(5), m.group(6)) if s]
        if len(components) == 1:
            components = [c for c in components[0]]  # Support notation of the form: py37

        if components and len(components) <= 3:
            self.version = Version(".".join(components))
            self.canonical = "%s:%s" % (family, self.version)

    def __repr__(self):
        return short(self.canonical)

    def __eq__(self, other):
        return isinstance(other, PythonSpec) and self.canonical == other.canonical

    def __lt__(self, other):
        if isinstance(other, PythonSpec):
            if self.family == other.family:
                if self.version:
                    return other.version and self.version < other.version

                return other.version or self.canonical < other.canonical

            return self.family < other.family


class PyInstallInfo:
    """Information on a python installation, determined dynamically when needed"""

    def __init__(self, version=None, sys_prefix=None, base_prefix=None, problem=None):
        self.version = Version(version) if version else None
        self.sys_prefix = sys_prefix
        self.base_prefix = base_prefix
        if not problem and not self.version.is_valid:
            problem = "unknown version '%s'" % self.version

        self.problem = problem


class PythonDepot(object):
    """
    Scan usual locations to discover python installations.
    2 types of location are scanned:
    - pyenv-like folders (very quick scan, scanned immediately)
    - PATH-like env vars (slower scan, scanned as late as possible)
    - 'locations' is accepted as a ':'-separated string, to make configuration easier

    Example usage:
        my_depot = PythonDepot(locations="~/.pyenv:$PATH")
        p = my_depot.find_python("3.7")
    """

    available = None  # type: list[PythonInstallation]  # Available installations (used to find pythons by spec)
    invalid = None  # type: list[PythonInstallation]  # Invalid python installations found
    invoker = None  # type: PythonInstallation
    _cache = None  # type: dict[str, PythonInstallation]
    _path_scanned = False

    def __init__(self, pyenv="~/.pyenv", use_path=True):
        """
        Args:
            pyenv (str | list[str] | None): pyenv-like installations to scan (multiple possible, ':'-separated)
            use_path (bool): If True, scan $PATH for python installations as well (this is done "as late as possible")
        """
        self.pyenv = pyenv
        self.use_path = use_path
        self.families = Families()
        self.origin = Origins()
        self.rescan()

    def rescan(self, scan_path=False):
        """Rescan configured locations for python installations

        Args:
            scan_path (bool): If True, scan PATH env var immediately
        """
        self.available = []
        self.invalid = []
        self._cache = {}
        self._path_scanned = not self.use_path
        self._scan_pyenv()
        if scan_path:
            self.scan_path_env_var(sort=False)

        base_prefix = getattr(sys, "real_prefix", None) or getattr(sys, "base_prefix", sys.prefix)
        self.invoker = self._cache.get(base_prefix) or self._find_invoker(base_prefix)
        self.invoker.is_invoker = True
        self._cache["invoker"] = self.invoker
        self._sort()

    def _find_invoker(self, base_prefix):
        info = PyInstallInfo(sys.version.partition(" ")[0], sys.prefix, base_prefix)
        equivalents = set()
        exe = None
        for path in self.python_exes_in_folder(info.base_prefix, major=info.version.major):
            equivalents.add(path)
            equivalents.add(os.path.realpath(path))
            if exe is None:
                exe = path

        if not exe:
            exe = os.path.realpath(sys.executable)

        for path in equivalents:
            python = self._cache.get(path)
            if python and not python.problem:
                return python

        spec = self.spec_from_text(info.version.text, exe)
        python = PythonInstallation(exe, origin=self.origin.path, equivalents=equivalents, spec=spec)
        self._register(python)
        return python

    def find_python(self, spec, fatal=False, logger=UNSET):
        """
        Args:
            spec (str | PythonSpec | None): Example: 3.7, py37, pypy3.7, conda3.7, /usr/bin/python
            fatal (bool | None): True: abort execution on failure, False: don't abort but log, None: don't abort, don't log
            logger (callable | None): Logger to use, False to log errors only, None to disable log chatter

        Returns:
            (PythonInstallation): Object representing python installation (may not be usable, see reported .problem)
        """
        if not isinstance(spec, PythonSpec):
            python = self._cache.get(spec)
            if python:
                return self._checked_pyinstall(python, fatal)

            spec = self.spec_from_text(spec)

        python = self._cache.get(spec.canonical)
        if python:
            return self._checked_pyinstall(python, fatal)

        if _is_path(spec.canonical):
            # Path reference: look it up and remember it "/"
            exe = self.resolved_python_exe(spec.canonical, major=sys.version_info[0])
            if not exe:
                python = PythonInstallation(spec.canonical, origin=self.origin.adhoc, problem="not an executable")
                self._register(python)
                return self._checked_pyinstall(python, fatal)

            python = self._cache.get(exe) or self._cache.get(os.path.realpath(exe))
            if python:
                return self._checked_pyinstall(python, fatal)

            python = self._python_from_path(exe, self.origin.adhoc)
            return self._checked_pyinstall(python, fatal)

        for python in self.available:
            if python.satisfies(spec):
                return python

        from_path = self.scan_path_env_var(logger=logger)
        if from_path:
            for python in from_path:
                if python.satisfies(spec):
                    return python

        python = PythonInstallation(spec.text, origin=self.origin.adhoc, spec=spec, problem="not available")
        self._register(python)
        return self._checked_pyinstall(python, fatal)

    def scan_path_env_var(self, logger=UNSET, sort=True):
        """Ensure env vars locations are scanned

        Args:
            logger (callable | None): Logger to use, False to log errors only, None to disable log chatter
            sort (bool): Internal, used to minimize number of times self.available gets sorted

        Returns:
            (list[PythonInstallation] | None): Installations
        """
        if self._path_scanned:
            return None

        self._path_scanned = True
        found = []
        real_paths = defaultdict(set)
        for folder in flattened(os.environ.get("PATH"), split=os.pathsep, keep_empty=None):
            for path in self.python_exes_in_folder(folder):
                real_path = os.path.realpath(path)
                if real_path not in self._cache:
                    real_paths[real_path].add(path)

        for real_path, paths in real_paths.items():
            python = self._python_from_path(real_path, self.origin.path, equivalents=paths, sort=False)
            if python.origin is self.origin.path:
                found.append(python)

        _R.hlog(logger, "Found %s pythons in $PATH" % (len(found)))
        if sort and found:
            self._sort()

        return sorted(found, reverse=True)

    def _cached_equivalents(self, python):
        cached = False
        for p in python.equivalent:
            if p not in self._cache:
                self._cache[p] = python
                cached = True

        return cached

    def spec_from_text(self, text, family=None):
        """
        Args:
            text (str): Given text describing a desired python
            family (PrioritizedName | str | None): Optional alternative text to examine to guess family

        Returns:
            (PythonSpec): Object formalizing how that spec is handled internally
        """
        if not isinstance(family, PrioritizedName):
            family = self.families.guess_family(family or text)

        return PythonSpec(text, family)

    @staticmethod
    def python_exes_in_folder(path, major=None):
        """
        Args:
            path (str): Path to python exe or folder with a python installation
            major (int | None): Optional, major version to search for

        Returns:
            Yields all python executable names
        """
        if path:
            path = resolved_path(path)
            if os.path.isdir(path):
                bin_folder = os.path.join(path, "bin")
                if os.path.isdir(bin_folder):
                    path = bin_folder

                names = ("python%s" % major, "python") if major else ("python", "python3", "python2")
                for name in names:
                    candidate = os.path.join(path, name)
                    if is_executable(candidate):
                        yield candidate

            elif is_executable(path):
                yield path

    def resolved_python_exe(self, path, major=None):
        """Find python executable from 'path'
        Args:
            path (str): Path to a bin/python, or a folder containing bin/python
            major (int | None): Optional, major version to search for

        Returns:
            (str): Full path to bin/python, if any
        """
        for exe in self.python_exes_in_folder(path, major=major):
            return exe

    def _sort(self):
        self.available = sorted(self.available, reverse=True)

    def _python_from_path(self, path, origin, equivalents=None, sort=True):
        """
        Args:
            path (str): Path to python executable
            origin (PrioritizedName): Origin that triggered the scan
            equivalents (list | set | None): Additional equivalent paths
            sort (bool): Internal, used to minimize number of times self.available gets sorted

        Returns:
            (PythonInstallation): Corresponding python installation
        """
        info = _Introspect.scan_exe(path)
        if info.problem:
            python = PythonInstallation(path, origin=origin, equivalents=equivalents, problem=info.problem)
            self._register(python)
            return python

        spec = self.spec_from_text(info.version.text, path)
        if info.sys_prefix != info.base_prefix:
            # We have a venv, return parent python
            exe = self.resolved_python_exe(info.base_prefix, major=info.version.major)
            python = self._cache.get(exe)
            if python:
                return python

            if not equivalents:
                equivalents = self.python_exes_in_folder(os.path.dirname(path), major=info.version.major)

            path = exe

        python = PythonInstallation(path, origin=origin, equivalents=equivalents, spec=spec)
        if self._register(python):
            if sort:
                self._sort()

        return python

    def _scan_pyenv(self, logger=UNSET):
        for location in flattened(self.pyenv, split=":", keep_empty=None):
            location = resolved_path(location)
            if location and os.path.isdir(location):
                count = 0
                pv = os.path.join(location, "versions")
                if os.path.isdir(pv):
                    location = pv

                for fname in os.listdir(location):
                    folder = os.path.join(location, fname)
                    spec = self.spec_from_text(fname, folder)
                    if spec.version:
                        exes = list(self.python_exes_in_folder(folder))
                        problem = None
                        if not exes:
                            problem = "invalid pyenv installation"

                        exes.append(folder)
                        python = PythonInstallation(exes[0], origin=self.origin.pyenv, equivalents=exes, spec=spec, problem=problem)
                        count += self._register(python)

                _R.hlog(logger, "Found %s pythons in %s" % (count, short(location)))

    def _register(self, python):
        """
        Args:
            python (PythonInstallation): Python installation to register

        Returns:
            (int): 1 if registered (0 if python was invalid, or already known)
        """
        if self._cached_equivalents(python):
            if not python.problem:
                self.available.append(python)
                return 1

            self.invalid.append(python)

        return 0

    @staticmethod
    def _checked_pyinstall(python, fatal):
        """Optionally abort if 'python' installation is not valid"""
        if fatal and python.problem:
            abort("Invalid python installation: %s" % python)

        return python


class Version(object):
    """
    Parse versions according to PEP-0440, ordering for non pre-releases is well supported
    Pre-releases are partially supported, no complex combinations (such as ".post.dev") are paid attention to
    """

    def __init__(self, text, max_parts=4):
        """
        Args:
            text (str | None): Text to be parsed
            max_parts (int): Maximum number of parts (components) to consider version valid
        """
        self.text = text or ""
        self.components = None
        self.prerelease = None
        m = R_VERSION.match(self.text)
        if not m:
            return

        self.text, major, main_part, pre, pre_num, rel, rel_num = m.group(1, 2, 3, 7, 8, 10, 11)
        if rel:
            rel = rel.lower()

        components = (major + main_part).split(".")
        if len(components) > max_parts:
            return  # Invalid version

        while len(components) < max_parts:
            components.append(0)

        if rel in ("final", "post"):
            components.append(rel_num or 0)

        else:
            components.append(0)

        self.components = tuple(map(int, components))
        pre = "dev" if rel == "dev" else "_" + pre if pre else None  # Ensure 'dev' is sorted higher than other pre-release markers
        if pre:
            self.prerelease = (pre, int(pre_num or 0))

    @classmethod
    def from_text(cls, text):
        v = cls(text)
        if v.is_valid:
            return v

    def __repr__(self):
        return self.text

    def __hash__(self):
        return hash(self.text)

    def __eq__(self, other):
        return isinstance(other, Version) and self.components == other.components and self.prerelease == other.prerelease

    def __lt__(self, other):
        if isinstance(other, Version):
            if self.components is None or other.components is None:
                return other.components

            if self.components == other.components:
                if self.prerelease:
                    return other.prerelease and self.prerelease < other.prerelease

                return other.prerelease

            return self.components < other.components

    @property
    def is_valid(self):
        return self.components is not None

    @property
    def main(self):
        if self.components:
            return "%s.%s.%s" % (self.major, self.minor, self.patch)

    @property
    def major(self):
        return self.components and self.components[0]

    @property
    def minor(self):
        return self.components and self.components[1]

    @property
    def patch(self):
        return self.components and self.components[2]


class PythonInstallation(object):
    """Models a specific python installation"""

    equivalent = None  # type: set[str] # Paths that are equivalent to this python installation
    executable = None  # type: str # Full path to python executable
    family = None  # type: PrioritizedName
    is_invoker = False  # Is this the python we're currently running under?
    problem = None  # type: str # String describing a problem with this installation, if there is one
    spec = None  # type: PythonSpec # Corresponding spec
    origin = None  # type: PrioritizedName # Where this installation came from (pyenv, invoker, PATH, ...)

    def __init__(self, exe, origin=None, equivalents=None, spec=None, problem=None):
        """
        Args:
            exe (str): Path to executable
            origin (PrioritizedName): Where this installation came from (pyenv, invoker, PATH, ...)
            equivalents (list | set | None): Optional equivalent identifiers for this installation
            spec (PythonSpec | None): Associated spec
            problem (str | None): Problem with this installation, if any
        """
        self.executable = _simplified_python_path(exe)
        self.location = self.executable
        if "pyenv" in self.location:
            self.location = os.path.dirname(os.path.dirname(self.location))

        self.origin = origin
        self.spec = spec
        self.problem = problem
        self.equivalent = {exe}
        if not problem:
            self.equivalent.add(os.path.realpath(exe))

        if self.executable != exe:
            self.equivalent.add(self.executable)
            if not problem:
                self.equivalent.add(os.path.realpath(self.executable))

        if equivalents:
            self.equivalent.update(equivalents)

    def __repr__(self):
        return self.representation(colored=False, canonical=True, origin=False)

    def __eq__(self, other):
        return isinstance(other, PythonInstallation) and self.executable == other.executable

    def __ne__(self, other):
        return not isinstance(other, PythonInstallation) or self.executable != other.executable

    def __lt__(self, other):
        if isinstance(other, PythonInstallation):
            if self.origin == other.origin:
                if self.spec:
                    return other.spec and self.spec < other.spec

                return other.spec

            return self.origin < other.origin

    @property
    def family(self):
        """Python family"""
        return self.spec and self.spec.family

    @property
    def major(self):
        """Major python version, if any"""
        return self.spec and self.spec.version and self.spec.version.major

    @property
    def version(self):
        """Python version, if any"""
        return self.spec and self.spec.version

    def representation(self, colored=True, canonical=True, origin=True):
        """Colored textual representation of this python installation"""
        bold = dim = orange = red = str
        if colored:
            rm = _R._runez_module()
            bold, dim, orange, red = rm.bold, rm.dim, rm.orange, rm.red

        note = []
        if canonical:
            note.append(red(self.problem) if self.problem else dim(self.spec.canonical))

        if origin:
            note.append(orange(dim(self.origin)))
            if self.is_invoker:
                note.append(orange("invoker"))

        text = bold(short(self.location))
        if note:
            text += " [%s]" % ", ".join(note)

        return text

    def satisfies(self, spec):
        """
        Args:
            spec (PythonSpec): Spec expressed by user or configuration

        Returns:
            (bool): True if this python installation satisfies it
        """
        if not self.problem:
            return spec.canonical in self.equivalent or self.spec.canonical.startswith(spec.canonical)


class _Introspect(object):
    """Introspect a python installation via the built-in `_pv.py` script"""

    _pv = None

    @classmethod
    def scan_exe(cls, exe):
        """
        Args:
            exe (str): Path to python executable

        Returns:
            (PyInstallInfo): Extracted info
        """
        r = run(exe, cls.get_pv(), dryrun=False, fatal=False, logger=None)
        if not r.succeeded:
            return PyInstallInfo(problem=short(r.full_output))

        try:
            lines = r.output.strip().splitlines()
            if len(lines) != 3:
                return PyInstallInfo(problem="introspection yielded %s lines instead of 3" % len(lines))

            version, sys_prefix, base_prefix = lines
            return PyInstallInfo(version, sys_prefix, base_prefix)

        except Exception as e:  # pragma: no cover
            return PyInstallInfo(problem="introspection error: %s" % short(e))

    @classmethod
    def get_pv(cls):
        if cls._pv is None:
            cls._pv = os.path.dirname(os.path.abspath(__file__))
            cls._pv = os.path.join(cls._pv, "_pv.py")

        return cls._pv
