import inspect
import os
import tarfile

from abc import ABC, abstractmethod
from importlib import import_module
from functools import wraps
from pkgutil import iter_modules, walk_packages
from typing import Any, BinaryIO, Callable, List, Optional, Tuple, Union

TestResult = Union[int, Tuple[int, dict]]


class InvalidTestMethod(Exception):
    """Indicates that a method marked as a test does not fulfill the necessary
    requirements."""

    pass


def test(name: str, description: str) -> Callable:
    """Decorator that marks the given method as a test.
    
    It forces the decorated method to return just an int representing the
    result code of the test execution or an additional dictionary containing
    any extra information that the developer wishes to provide. Also, the
    following dictionary structure is now returned by the method:

    'test_name': a string filled with the given name parameter.
    
    'test_description': a string filled with the given description parameter.

    'result_code': a number that represents the success (> 0), failure (< 0) or
    unexpected condition (0) after the test execution.

    'additional_info': an optional dictionary containing extra relevant data.

    Parameters
    ----------
    name: str
        The name given to the test.
    description:
        A brief explanation of the test.

    Returns
    -------
    Callable
        The decorated method.
    """

    def test_decorator(
            method: Callable[[TestSet], TestResult]) -> Callable:
        @wraps(method)
        def wrapper(self: TestSet) -> Callable:
            report = {}
            result = method(self)

            if isinstance(result, int):
                report['result_code'] = result
            elif not isinstance(result, tuple):
                raise InvalidTestMethod("Invalid return type.")
            elif len(result) != 2:
                raise InvalidTestMethod("Incorrect number of return values.")
            elif not isinstance(result[0], int):
                raise InvalidTestMethod("Result code is not an integer.")
            elif not isinstance(result[1], dict):
                raise InvalidTestMethod(
                    "Second return value is not a dictionary.")
            else:
                report['result_code'] = result[0]
                report['additional_info'] = result[1]

            report['test_name'] = name
            report['test_description'] = description
            return report

        wrapper.test = True
        return wrapper
    
    return test_decorator

def is_test(x: Any) -> bool:
    """Returns wheter the argument is a test method.
    
    Its use is recommended when inspecting a class definition, as there is no
    instance bound to the method.
    """

    return inspect.isfunction(x) and hasattr(x, 'test')

def is_test_method(x: Any) -> bool:
    """Returns wheter the argument is a test in the form a method bound to an
    object."""

    return inspect.ismethod(x) and hasattr(x, 'test')


class TestSet(ABC):
    """Base class that provides a common interface for all test sets."""

    @staticmethod
    def is_strict_subclass(x: Any) -> bool:
        """Returns wheter the argument is a subclass of TestSet but not TestSet
        itself."""
        return (inspect.isclass(x)
            and issubclass(x, TestSet)
            and x is not TestSet)

    def __init__(self, description: str):
        self.description = description

    def run(self) -> List[dict]:
        """Executes all given tests in the set.
        
        Returns
        -------
        List
            A list containing the individual reports generated by each test.
            The structure of the report is documented in the test decorator.
        """
        results = []
        tests = inspect.getmembers(self, is_test_method)
        for _, method in tests:
            try:
                results.append(method())
            except InvalidTestMethod as e:
                print(str(e))
        return results


class TestSetCollection():
    """Loads all the tests found in the corresponding packages."""

    def __init__(self,
            tests_root: str,
            packages: List[str] = [],
            modules: List[str] = [],
            test_sets: List[str] = []):
        self.tests_root = tests_root
        if packages or modules or test_sets:
            self.load_entities(packages, modules, test_sets)
        else:
            self.test_sets: List[TestSet] = []
            self.load_package(self.tests_root)

    def load_entities(
            self,
            packages: List[str] = [],
            modules: List[str] = [],
            test_sets: List[str] = []) -> None:
        """Recovers all test sets from the given packages."""

        self.test_sets: List[TestSet] = []
        
        for package in packages:
            self.load_package(f"{self.tests_root}.{package}")
        
        for module in modules:
            self.load_module(f"{self.tests_root}.{module}")

        for ts in test_sets:
            module, c = ts.rsplit(".", 1)
            self.load_test_set(f"{self.tests_root}.{module}", c)

    def load_package(self, package: str) -> None:
        """Looks for all the test sets in the given package and its
        subpackages."""
        if isinstance(package, str):
            package = import_module(package)
        for _, name, is_pkg in walk_packages(
                package.__path__,
                package.__name__ + '.'):
            if not is_pkg:
                self.load_module(name)

    def load_module(self, module: str) -> None:
        mod = import_module(module)
        classes = inspect.getmembers(mod, TestSet.is_strict_subclass)
        for _, c in classes:
            self.test_sets.append(c())

    def load_test_set(self, module: str, test_set: str):
        mod = import_module(module)
        c = getattr(mod, test_set)
        if TestSet.is_strict_subclass(c):
            self.test_sets.append(c())
        else:
            raise ValueError(f"{test_set} is not a valid class.")

    def run_all_tests(self) -> List[List[dict]]:
        results = []
        for ts in self.test_sets:
            results += ts.run()
        return results


def get_installed_package(package_name: str) -> dict:
    """Recovers information about the given package.

    The returned dictionary contains the following keys:

    1. 'name': the package base name.

    2. 'subpackages': a list of dictionaries with a recursive format
    representing the subpackages found.

    3. 'modules': a list of dictionaries representing the found modules within
    the package. They have the following keys:

    3.1 'name': the name of the module.
    
    3.2 'test_sets': a list of dictionaries representing the classes extended
    from TestSet found in the given module. They contain the following keys:
    
    3.2.1 'name': the name of the class.
    
    3.2.2 'tests': a list of the names of the test methods found within the
    class.

    Parameters
    ----------
    package_name : str
        The canonical name of the package to analyze.

    Returns
    -------
    dict
        A dictionary representing the structure of the given package. 
    """

    installed = {
        'name': package_name.split(".")[-1], # Basename only
        'subpackages': [],
        'modules': [],
    }
    package = import_module(package_name)
    # Looks for packages in package.__path__
    # All found entities are given their canonical name inheriting their
    # parent's
    for _, name, is_pkg in iter_modules(
            package.__path__,
            package.__name__ + '.'):
        if is_pkg:
            # Recursive call with the found subpackage
            sub = get_installed_package(name)
            installed['subpackages'].append(sub)
        else:
            module_info = {
                'name': name.split(".")[-1], # Basename only
                'test_sets': []
            }
            module = import_module(name)
            classes = inspect.getmembers(module, TestSet.is_strict_subclass)
            for class_name, c in classes:
                class_info = {
                    'name': class_name,
                    'tests': []
                }
                tests = inspect.getmembers(c, is_test)
                for test_name, _ in tests:
                    class_info['tests'].append(test_name)
                module_info['test_sets'].append(class_info)
            installed['modules'].append(module_info)
    return installed

def get_installed_test_sets(root_package: str) -> List[dict]:
    """Recovers information about the installed test sets at the given root
    package.

    Each component of the returned list is a dictionary of the form returned by
    the function get_installed_package().

    Parameters
    ----------
    root_package : str
        The name of the root package containing all other packages filled with
        test sets.

    Returns
    -------
    List[dict]
        A list whose components are dictionaries representing each of the found
        packages. 
    """

    package = import_module(root_package)
    installed = []
    # Looks for packages in package.__path__
    # All found entities are given their canonical name from the root package
    for _, name, is_pkg in iter_modules(
            package.__path__,
            package.__name__ + '.'):
        if is_pkg:
            installed.append(get_installed_package(name))
    return installed

def compress_test_packages(
        file_object: BinaryIO,
        test_packages: List[str],
        tests_root: str) -> None:
    """Compress the given packages at the root directory for tests.

    Only top level packages are allowed, everything else is ignored.
    Non-existent packages are also ignored. All pycache folders are not
    included in the resulting file. The compression format used is gzip.

    Parameters
    ----------
    file_object: BinaryIO
        A file like object in which the resulting file is generated.
    test_packages: List[str]
        A list of packages names.
    tests_root : str
        The root directory name where the tests sets packages are stored.
    """

    def filter_pycache(x):
        """Excludes x if it's a pycache directory."""
        if os.path.basename(x.name) == "__pycache__":
            return None
        else:
            return x

    with tarfile.open(fileobj=file_object, mode="w:gz") as tar:
        for tp in test_packages:
            if len(tp.split(".")) > 1:
                print(tp + "ignored. Only top level packages allowed.")
            else:
                tp_path = os.path.join(tests_root, tp)
                if os.path.isdir(tp_path):
                    tar.add(tp_path, tp, filter=filter_pycache)
                else:
                    print("No package found with name " + tp + ".")

def uncompress_test_packages(file_object: BinaryIO, tests_root: str) -> List[str]:
    """Uncompress the given file in the root directory for tests.

    The file must be in gzip format.

    Parameters
    ----------
    file_object: BinaryIO
        A file like object from which the tests sets are extracted.
    tests_root : str
        The root directory name where the extracted packages are going to be
        stored.
    
    Raises
    ------
    ValueError
        A top level member of the tar file that is not a package was found.

    Returns
    -------
    List[str]
        A list whose components are the names of the top level packages found.
    """

    def member_is_package(
            tar: tarfile.TarFile,
            member: tarfile.TarInfo) -> bool:
        """Checks if the given member of the provided tar object contains a
        __init__.py file.

        Parameters
        ----------
        tar: tarfile.TarFile
            A tar object containing member.
        member: tarfile.TarInfo
            The member of the tar object to verify.

        Returns
        -------
        bool
            Wheter the given member is a package or not.
        """
        try:
            tar.getmember(f"{member.name}/__init__.py")
            return True
        except KeyError:
            return False

    new_packages = []
    with tarfile.open(fileobj=file_object, mode="r:gz") as tar:
        for member in tar:
            if member.name.count("/") == 0: # It's a top level member
                if not (member.isdir() and member_is_package(tar, member)):
                    raise ValueError(
                        f"Found top level member {member} is not a package.")
                new_packages.append(member.name)
        tar.extractall(tests_root)
    return new_packages