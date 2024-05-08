import argparse
import platform
import shutil
from functools import total_ordering

import requests
import yaml
from urllib.parse import urlparse
import os
from zipfile import ZipFile, ZIP_DEFLATED
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import cpu_count
from tqdm import tqdm
import hashlib
import ast

VERSION = "v0.0.1"
CONAN_CENTER_INDEX_RECIPES_DIR = "conan-center-index-master/recipes/"
CONAN_CENTER_RECIPES_DIR = "recipes"
CONAN_CENTER_SOURCE_CACHE_DIR = "sources"
SOURCES_PREFIX = "_sources/"
REQUIRES_TREE_FILE = "_requires_tree_file.yml"
CONAN_DATA_YML = "conandata.yml"
CONAN_FILE_PY = "conanfile.py"
CONAN_CONFIG_YML = "config.yml"
# 缓存文件后缀
CACHE_TEMPORARY_SUFFIX = ".tmp"
# 用于优先构建的特殊包名
FIRST_BUILD_PACKAGE = ["msys2", "ninja", "m4",
                       "autoconf", "gnu-config", "automake",
                       "strawberryperl", "meson", "pkgconf", "libtool"]
# windows 平台专用包名
WINDOWS_ONLY_PACKAGE = {"msys2", "strawberryperl"}
# linux 平台专用包名
LINUX_ONLY_PACKAGE = {"linux-headers-generic", "libmount", "libsystemd",
                      "egl", "libalsa", "libcap", "libselinux"}


def sha256_file(file_path):
    hash_object = hashlib.sha256()
    with open(file_path, "rb") as file:
        chunk = file.read(4096)
        while len(chunk) > 0:
            hash_object.update(chunk)
            chunk = file.read(4096)

    return hash_object.hexdigest()


def clear_cache_tmp(cache_dir):
    for root, _, files in os.walk(cache_dir):
        for file in files:
            if file.endswith(CACHE_TEMPORARY_SUFFIX):
                os.remove(os.path.join(root, file))


def remove_prefix(s, prefix):
    if s.startswith(prefix):
        return s[len(prefix):]
    else:
        return s


def safe_next(it):
    try:
        return next(it)
    except StopIteration:
        return None


def load_yaml(file_path):
    with open(file_path, "r") as f:
        return yaml.safe_load(f)


def get_dir_by_link(parent_dir, link):
    parsed_url = urlparse(link)
    return os.path.join(parent_dir, parsed_url.netloc + parsed_url.path)


def download(file, link):
    hash_object = hashlib.sha256()
    rsp = requests.get(link, headers=None, stream=True)
    if rsp.status_code == 200:
        # 文件开始下载后，可以按需处理这些数据
        for chunk in rsp.iter_content(chunk_size=64 * 1024):
            if chunk:  # filter out keep-alive new chunks
                hash_object.update(chunk)
                file.write(chunk)
        return hash_object.hexdigest()
    else:
        raise Exception(f"Request failed with status code: {rsp.status_code}")


def conan_exist_package(package, remote):
    return os.popen(f"conan list \"{package}\" -r={remote}").read().find("not found") == -1


def collect_requires(collect, conan_file_py_path):
    with open(conan_file_py_path, "r", encoding="utf-8") as f:
        node = ast.parse(f.read())
    collect_func_name = {"requires", "tool_requires", "build_requires", "test_requires"}
    for node in ast.walk(node):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.attr in collect_func_name and func.value.id == "self" and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant):
                        collect(arg.value)


def is_windows():
    return platform.system() == "Windows"


class ZipTool:
    def __init__(self, path, compress_level=3):
        self.zip_file = ZipFile(path, "w", ZIP_DEFLATED, compresslevel=compress_level)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.zip_file.close()

    def add_file(self, file_path: str, start_path: str, prefix=None, tq=None):
        if prefix is None:
            arc_name = os.path.sep + os.path.relpath(file_path, start_path)
        else:
            arc_name = prefix + os.path.relpath(file_path, start_path)
        self.zip_file.write(file_path, arc_name)
        if tq:
            tq.set_postfix({"打包文件": arc_name})
        return arc_name

    def add_dir(self, dir_path: str, start_path: str, tq: None):
        for root, _, files in os.walk(dir_path):
            for file in files:
                file = os.path.join(root, file)
                self.add_file(file, start_path, tq=tq)

    def writestr(self, name, data):
        self.zip_file.writestr(name, data)


class _VersionItem:
    """ a single "digit" in a version, like X.Y.Z all X and Y and Z are VersionItems
    They can be int or strings
    """

    def __init__(self, item):
        try:
            self._v = int(item)
        except ValueError:
            self._v = item

    @property
    def value(self):
        return self._v

    def __str__(self):
        return str(self._v)

    def __add__(self, other):
        # necessary for the "bump()" functionality. Other aritmetic operations are missing
        return self._v + other

    def __eq__(self, other):
        if not isinstance(other, _VersionItem):
            other = _VersionItem(other)
        return self._v == other._v

    def __hash__(self):
        return hash(self._v)

    def __lt__(self, other):
        """
        @type other: _VersionItem
        """
        if not isinstance(other, _VersionItem):
            other = _VersionItem(other)
        try:
            return self._v < other._v
        except TypeError:
            return str(self._v) < str(other._v)


@total_ordering
class Version:
    """
    This is NOT an implementation of semver, as users may use any pattern in their versions.
    It is just a helper to parse "." or "-" and compare taking into account integers when possible
    """

    def __init__(self, value, qualifier=False):
        value = str(value)
        self._value = value
        self._build = None
        self._pre = None
        self._qualifier = qualifier  # it is a prerelease or build qualifier, not a main version

        if not qualifier:
            items = value.rsplit("+", 1)  # split for build
            if len(items) == 2:
                value, build = items
                self._build = Version(build, qualifier=True)  # This is a nested version by itself

            # split for pre-release, from the left, semver allows hyphens in identifiers :(
            items = value.split("-", 1)
            if len(items) == 2:
                value, pre = items
                self._pre = Version(pre, qualifier=True)  # This is a nested version by itself

        items = value.split(".")
        items = [_VersionItem(item) for item in items]
        self._items = tuple(items)
        while items and items[-1].value == 0:
            del items[-1]
        self._nonzero_items = tuple(items)

    def bump(self, index):
        """
        :meta private:
            Bump the version
            Increments by 1 the version field at the specified index, setting to 0 the fields
            on the right.
            2.5 => bump(1) => 2.6
            1.5.7 => bump(0) => 2.0.0

        :param index:
        """
        # this method is used to compute version ranges from tilde ~1.2 and caret ^1.2.1 ranges
        # TODO: at this moment it only works for digits, cannot increment pre-release or builds
        # better not make it public yet, keep it internal
        items = list(self._items[:index])
        try:
            items.append(self._items[index] + 1)
        except TypeError:
            raise Exception(f"Cannot bump '{self._value} version index {index}, not an int")
        items.extend([0] * (len(items) - index - 1))
        v = ".".join(str(i) for i in items)
        # prerelease and build are dropped while bumping digits
        return Version(v)

    def upper_bound(self, index):
        items = list(self._items[:index])
        try:
            items.append(self._items[index] + 1)
        except TypeError:
            raise Exception(f"Cannot bump '{self._value} version index {index}, not an int")
        items.extend([0] * (len(items) - index - 1))
        v = ".".join(str(i) for i in items)
        v += "-"  # Exclude prereleases
        return Version(v)

    @property
    def pre(self):
        return self._pre

    @property
    def build(self):
        return self._build

    @property
    def main(self):
        return self._items

    @property
    def major(self):
        try:
            return self.main[0]
        except IndexError:
            return None

    @property
    def minor(self):
        try:
            return self.main[1]
        except IndexError:
            return None

    @property
    def patch(self):
        try:
            return self.main[2]
        except IndexError:
            return None

    @property
    def micro(self):
        try:
            return self.main[3]
        except IndexError:
            return None

    def version_range(self):
        """ returns the version range expression, without brackets []
        or None if it is not an expression
        """
        version = repr(self)
        if version.startswith("[") and version.endswith("]"):
            return VersionRange(version[1:-1])

    def __str__(self):
        return self._value

    def __repr__(self):
        return self._value

    def __eq__(self, other):
        if other is None:
            return False
        if not isinstance(other, Version):
            other = Version(other, self._qualifier)

        return (self._nonzero_items, self._pre, self._build) == \
            (other._nonzero_items, other._pre, other._build)

    def __hash__(self):
        return hash((self._nonzero_items, self._pre, self._build))

    def __lt__(self, other):
        if other is None:
            return False
        if not isinstance(other, Version):
            other = Version(other)

        if self._pre:
            if other._pre:  # both are pre-releases
                return (self._nonzero_items, self._pre, self._build) < \
                    (other._nonzero_items, other._pre, other._build)
            else:  # Left hand is pre-release, right side is regular
                if self._nonzero_items == other._nonzero_items:  # Problem only happens if both equal
                    return True
                else:
                    return self._nonzero_items < other._nonzero_items
        else:
            if other._pre:  # Left hand is regular, right side is pre-release
                if self._nonzero_items == other._nonzero_items:  # Problem only happens if both equal
                    return False
                else:
                    return self._nonzero_items < other._nonzero_items
            else:  # None of them is pre-release
                return (self._nonzero_items, self._build) < (other._nonzero_items, other._build)


@total_ordering
class _Condition:
    def __init__(self, operator, version):
        self.operator = operator
        self.display_version = version

        value = str(version)
        if (operator == ">=" or operator == "<") and "-" not in value:
            value += "-"
        self.version = Version(value)

    def __str__(self):
        return f"{self.operator}{self.display_version}"

    def __repr__(self):
        return self.__str__()

    def __hash__(self):
        return hash((self.operator, self.version))

    def __lt__(self, other):
        # Notice that this is done on the modified version, might contain extra prereleases
        if self.version < other.version:
            return True
        elif self.version == other.version:
            if self.operator == "<":
                if other.operator == "<":
                    return self.display_version.pre is not None
                else:
                    return True
            elif self.operator == "<=":
                if other.operator == "<":
                    return False
                else:
                    return self.display_version.pre is None
            elif self.operator == ">":
                if other.operator == ">":
                    return self.display_version.pre is None
                else:
                    return False
            else:
                if other.operator == ">":
                    return True
                # There's a possibility of getting here while validating if a range is non-void
                # by comparing >= & <= for lower limit <= upper limit
                elif other.operator == "<=":
                    return True
                else:
                    return self.display_version.pre is not None
        return False

    def __eq__(self, other):
        return (self.display_version == other.display_version and
                self.operator == other.operator)


class _ConditionSet:

    def __init__(self, expression, prerelease):
        expressions = expression.split()
        self.prerelease = prerelease
        self.conditions = []
        for e in expressions:
            e = e.strip()
            self.conditions.extend(self._parse_expression(e))

    @staticmethod
    def _parse_expression(expression):
        if expression in ("", "*"):
            return [_Condition(">=", Version("0.0.0"))]
        elif len(expression) == 1:
            raise Exception(f'Error parsing version range "{expression}"')

        operator = expression[0]
        if operator not in (">", "<", "^", "~", "="):
            operator = "="
            index = 0
        else:
            index = 1
        if operator in (">", "<"):
            if expression[1] == "=":
                operator += "="
                index = 2
        version = expression[index:]
        if version == "":
            raise Exception(f'Error parsing version range "{expression}"')
        if operator == "~":  # tilde minor
            if "-" not in version:
                version += "-"
            v = Version(version)
            index = 1 if len(v.main) > 1 else 0
            return [_Condition(">=", v), _Condition("<", v.upper_bound(index))]
        elif operator == "^":  # caret major
            v = Version(version)

            def first_non_zero(main):
                for i, m in enumerate(main):
                    if m != 0:
                        return i
                return len(main)

            initial_index = first_non_zero(v.main)
            return [_Condition(">=", v), _Condition("<", v.upper_bound(initial_index))]
        else:
            return [_Condition(operator, Version(version))]

    def valid(self, version, conf_resolve_prepreleases):
        if version.pre:
            # Follow the expression desires only if core.version_ranges:resolve_prereleases is None,
            # else force to the conf's value
            if conf_resolve_prepreleases is None:
                if not self.prerelease:
                    return False
            elif conf_resolve_prepreleases is False:
                return False
        for condition in self.conditions:
            if condition.operator == ">":
                if not version > condition.version:
                    return False
            elif condition.operator == "<":
                if not version < condition.version:
                    return False
            elif condition.operator == ">=":
                if not version >= condition.version:
                    return False
            elif condition.operator == "<=":
                if not version <= condition.version:
                    return False
            elif condition.operator == "=":
                if not version == condition.version:
                    return False
        return True


class VersionRange:
    def __init__(self, expression):
        self._expression = expression
        tokens = expression.split(",")
        prereleases = False
        for t in tokens[1:]:
            if "include_prerelease" in t:
                if "include_prerelease=" in t:
                    print(
                        f'include_prerelease version range option in "{expression}" does not take an attribute, '
                        'its presence unconditionally enables prereleases')
                prereleases = True
                break
            else:
                t = t.strip()
                if len(t) > 0 and t[0].isalpha():
                    print(f'Unrecognized version range option "{t}" in "{expression}"')
                else:
                    raise Exception(f'"{t}" in version range "{expression}" is not a valid option')
        version_expr = tokens[0]
        self.condition_sets = []
        for alternative in version_expr.split("||"):
            self.condition_sets.append(_ConditionSet(alternative, prereleases))

    def __str__(self):
        return self._expression

    def contains(self, version, resolve_prerelease):
        """
        Whether <version> is inside the version range

        :param version: Version to check against
        :param resolve_prerelease: If ``True``, ensure prereleases can be resolved in this range
        If ``False``, prerelases can NOT be resolved in this range
        If ``None``, prereleases are resolved only if this version range expression says so
        :return: Whether the version is inside the range
        """
        assert isinstance(version, Version), type(version)
        for condition_set in self.condition_sets:
            if condition_set.valid(version, resolve_prerelease):
                return True
        return False

    def intersection(self, other):
        conditions = []

        def _calculate_limits(operator, lhs, rhs):
            limits = ([c for c in lhs.conditions if operator in c.operator]
                      + [c for c in rhs.conditions if operator in c.operator])
            if limits:
                return sorted(limits, reverse=operator == ">")[0]

        for lhs_conditions in self.condition_sets:
            for rhs_conditions in other.condition_sets:
                internal_conditions = []
                lower_limit = _calculate_limits(">", lhs_conditions, rhs_conditions)
                upper_limit = _calculate_limits("<", lhs_conditions, rhs_conditions)
                if lower_limit:
                    internal_conditions.append(lower_limit)
                if upper_limit:
                    internal_conditions.append(upper_limit)
                if internal_conditions and (not lower_limit or not upper_limit or lower_limit <= upper_limit):
                    conditions.append(internal_conditions)

        if not conditions:
            return None
        expression = ' || '.join(' '.join(str(c) for c in cs) for cs in conditions)
        result = VersionRange(expression)
        # TODO: Direct definition of conditions not reparsing
        # result.condition_sets = self.condition_sets + other.condition_sets
        return result

    def version(self):
        return Version(f"[{self._expression}]")


def load_require(requires):
    try:
        # timestamp
        tokens = requires.rsplit("%", 1)
        text = tokens[0]
        timestamp = float(tokens[1]) if len(tokens) == 2 else None

        # revision
        tokens = text.split("#", 1)
        ref = tokens[0]
        revision = tokens[1] if len(tokens) == 2 else None

        # name, version always here
        tokens = ref.split("@", 1)
        name, version = tokens[0].split("/", 1)
        # user and channel
        if len(tokens) == 2 and tokens[1]:
            tokens = tokens[1].split("/", 1)
            user = tokens[0] if tokens[0] else None
            channel = tokens[1] if len(tokens) == 2 else None
        else:
            user = channel = None
        return [name, Version(version), user, channel, revision, timestamp]
    except Exception:
        raise Exception(
            f"{requires} is not a valid recipe reference, provide a reference"
            f" in the form name/version[@user/channel]")


class ConanCenterIndex:
    def __init__(self, recipes_dir):
        self.recipes_dir = recipes_dir

    def get_recipe(self, name):
        return ConanRecipe(self, name)


def get_version_validator(version):
    version = Version(version)
    version_range = version.version_range()
    if version_range:
        return lambda v: version_range.contains(v, False)
    else:
        return lambda v: version == v


def get_versions_validator(version_list):
    version_validators = []
    for version in version_list:
        version_validators.append(get_version_validator(version))

    def _validator_func(value):
        for validator in version_validators:
            if validator(value):
                return True
        return False

    return _validator_func


class ConanRecipe:
    def __init__(self, index, name):
        self.index = index
        self.name = name
        self.recipe_dir = os.path.join(index.recipes_dir, name)
        self.conf = load_yaml(self.get_conf_path())

    def get_conf_path(self):
        return os.path.join(self.recipe_dir, CONAN_CONFIG_YML)

    def versions(self, validator):
        versions = self.conf.get("versions")
        if versions is None:
            return None
        for version in versions:
            version = Version(version)
            if validator(version):
                yield version

    def get_recipe_dir(self, version):
        if isinstance(version, Version):
            version = repr(version)
        return os.path.join(self.recipe_dir, self.conf["versions"][version]["folder"])

    def export_to_zip(self, zt, version_list, tq=None):
        zipped = set()
        validator = get_versions_validator(version_list)
        for version in self.versions(validator):
            recipe_dir = self.get_recipe_dir(version)
            if recipe_dir in zipped:
                continue
            zipped.add(recipe_dir)
            for file_name in os.listdir(recipe_dir):
                if file_name == CONAN_DATA_YML:
                    continue
                file_path = os.path.join(recipe_dir, file_name)
                if os.path.isdir(file_path):
                    zt.add_dir(file_path, self.index.recipes_dir, tq=tq)
                else:
                    zt.add_file(file_path, self.index.recipes_dir, tq=tq)

        zt.add_file(self.get_conf_path(), self.index.recipes_dir, tq=tq)


class ConanResource:
    def __init__(self, source):
        self.source = source
        self.url = source.get("url")
        self.sha256 = source.get("sha256")
        if self.url:
            if isinstance(self.url, str):
                self.url = [self.url]
        else:
            self.url = []

    def download(self, cache_dir):
        for i, url in enumerate(self.url):
            source_path = get_dir_by_link(cache_dir, url)
            if os.path.exists(source_path):
                local_sha256 = sha256_file(source_path)
                if local_sha256 != f"{self.sha256}".lower():
                    raise Exception("failed to check sum source file, except sha256 %s, but got %s: %s" %
                                    (self.sha256, local_sha256, source_path))
                return url, source_path
            source_path_parent = os.path.dirname(source_path)
            os.makedirs(source_path_parent, exist_ok=True)
            source_path_tmp = f"{source_path}{CACHE_TEMPORARY_SUFFIX}"
            try:
                with open(source_path_tmp, "wb") as file:
                    local_sha256 = download(file, url)
                if local_sha256 != f"{self.sha256}".lower():
                    raise Exception("failed to check sum download file, except sha256 %s, but got %s: %s" %
                                    (self.sha256, local_sha256, url))
                try:
                    os.rename(source_path_tmp, source_path)
                except FileExistsError:
                    pass
                return url, source_path
            except Exception as error:
                if i != len(self.url) - 1:
                    print(f"Could not download from the URL {url}: {error}.")
                    print("Trying another mirror.")
                else:
                    raise
            finally:
                try:
                    os.remove(source_path_tmp)
                except FileNotFoundError:
                    pass
        return None, None

    def set_url(self, url):
        self.source["url"] = url
        self.url = url

    def get_url(self):
        if self.url:
            return self.url[0]
        else:
            return None


def find_and_add_link_item(result, data):
    if isinstance(data, dict):
        keys = data.keys()
        if len(keys) == 2 and 'url' in keys and 'sha256' in keys:
            result.append(ConanResource(data))
            return
        else:
            data = data.values()
    elif isinstance(data, (list, tuple)):
        pass
    else:
        return
    for source in data:
        find_and_add_link_item(result, source)


class ConanData:
    def __init__(self, data):
        self.data = data

    @staticmethod
    def get_data_file_path(recipe_dir):
        return os.path.join(recipe_dir, CONAN_DATA_YML)

    @staticmethod
    def load(recipe_dir):
        return ConanData(load_yaml(ConanData.get_data_file_path(recipe_dir)))

    @staticmethod
    def get_requires(recipe_dir):
        result = set()

        def _collect(name):
            result.add(name)

        collect_requires(_collect, os.path.join(recipe_dir, CONAN_FILE_PY))
        return result

    def remove_source_by_validator(self, version_validator):
        sources = self.data.get("sources")
        result = []
        if sources:
            for version in sources.keys():
                if not version_validator(version):
                    result.append(version)
            for version in result:
                del sources[version]

    def get_sources(self):
        result = []
        find_and_add_link_item(result, self.data.get("sources"))
        return result

    def dump(self):
        return yaml.dump(self.data)


class ConanRecipeDownloader:
    def __init__(self, max_workers, cache_dir):
        self.cache_dir = cache_dir
        self.pool = ThreadPoolExecutor(max_workers=max_workers)

    def download_source(self, source):
        url, source_path = source.download(self.cache_dir)
        if url:
            source.set_url(url)
        return url, source_path

    def download_versions(self, recipe, zt, versions, tq=None):
        if tq:
            tq.set_postfix({"name": recipe.name}, refresh=True)
        validator = get_versions_validator(versions)
        versions = []
        conan_data = {}
        for version in recipe.versions(validator):
            versions.append(version)
            recipe_dir = recipe.get_recipe_dir(version)
            if conan_data.get(recipe_dir) is not None:
                continue
            try:
                conan_data[recipe_dir] = ConanData.load(recipe_dir)
            except FileNotFoundError as _:
                pass
        validator = get_versions_validator(versions)
        download_tasks = []
        for data in conan_data.values():
            data.remove_source_by_validator(validator)
            for source in data.get_sources():
                download_tasks.append(self.pool.submit(self.download_source, source))
        for task in as_completed(download_tasks):
            url, source_path = task.result()
            if tq:
                tq.set_postfix({"name": recipe.name, "url": url}, refresh=True)
            zt.add_file(source_path, self.cache_dir, prefix=SOURCES_PREFIX, tq=tq)
            if tq:
                tq.update(1)
        for recipe_dir in conan_data:
            data = conan_data.get(recipe_dir)
            zt.writestr(os.path.relpath(os.path.join(recipe_dir, CONAN_DATA_YML), recipe.index.recipes_dir),
                        data.dump())
        return versions


class RequireTree:
    def __init__(self, tree=None):
        if tree:
            self.tree = tree
        else:
            self.tree = {}

    @staticmethod
    def load(recipes_dir):
        return RequireTree(load_yaml(os.path.join(recipes_dir, REQUIRES_TREE_FILE)))

    def exist_node(self, name):
        return self.tree.get(name) is not None

    def get_node(self, name):
        return RequireTreeNode(self, name)

    def remove_node(self, name):
        del self.tree[name]
        for node in self.nodes():
            node.remove_require(name)

    def nodes(self):
        for name in self.tree:
            yield RequireTreeNode(self, name)

    def dumps(self):
        return yaml.dump(self.tree)


class RequireTreeNode:
    def __init__(self, root, name):
        self.name = name
        self.data = root.tree.get(name)
        if not self.data:
            self.ignore = False
            self.requires = []
            self.versions = []
            self.args = {}
            root.tree[name] = {
                "ignore": self.ignore,
                'requires': self.requires,
                'versions': self.versions,
                'args': self.args
            }
        else:
            self.ignore = self.data['ignore']
            self.requires = self.data['requires']
            self.versions = self.data['versions']
            self.args = self.data.get('args')

    def set_ignore(self, val):
        if self.ignore != val:
            self.data['ignore'] = val
            self.ignore = val

    def add_version(self, version):
        if version not in self.versions:
            version = str(version)
            self.versions.append(version)
            self.args[version] = ""
            print(f"需要下载包: {self.name}/{version}")

    def has_version(self, check_func):
        for version in self.versions:
            if check_func(Version(version)):
                return True
        return False

    def check_and_remove_exist_version(self, remote):
        for version in self.versions:
            if conan_exist_package(f"{self.name}/{version}", remote):
                self.remove_version(version)

    def remove_version(self, version):
        if version in self.versions:
            self.versions.remove(version)

    def remove_require(self, require):
        if require in self.requires:
            self.requires.remove(require)

    def add_require(self, require):
        if require != self.name and require not in self.requires:
            self.requires.append(require)
            print(f"包 {self.name} 依赖 {require}")

    def get_args(self, version):
        if self.args:
            version = str(version)
            ops = self.args.get(version)
            if ops:
                return ops
        return ""

    def build_and_upload(self, remote, recipe, tq, export_only=False):
        tq.set_description(f"正在构建 {self.name}")
        for version in self.versions:
            package_name = f"{self.name}/{version}"
            tq.set_postfix({"building": version})
            work_dir = recipe.get_recipe_dir(version)
            ops = self.get_args(version)
            if export_only:
                code = os.system(f"conan export \"{work_dir}\" --version={version} --name={self.name} {ops}")
            else:
                code = os.system(
                    f"conan create \"{work_dir}\" --version={version} --name={self.name} {ops} --build=missing")
            if code != 0:
                raise Exception(f"构建 {package_name} 失败了, 错误退出码 {code}: {work_dir}")
            else:
                tq.set_postfix({"uploading": version}, refresh=True)
                code = os.system(f"conan upload {package_name} -r={remote}")
            if code != 0:
                raise Exception(
                    f"执行上传构建 {package_name} 到远程仓库 {remote} 失败, 错误退出码 {code}: {work_dir} ")
            tq.update()


class ConanRecipeWithRequiresDownloader:
    def __init__(self, recipes_dir, source_cache_dir, output_path, max_workers, compress_level=3):
        self.index = ConanCenterIndex(recipes_dir)
        self.downloader = ConanRecipeDownloader(max_workers, source_cache_dir)
        self.zip_tool = ZipTool(output_path, compress_level=compress_level)
        self.requires_tree = RequireTree()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.zip_tool.__exit__(exc_type, exc_val, exc_tb)

    def collect_all_requires(self, requires, parent=None, visited=None, is_child=True):
        if visited is None:
            visited = set()
        if parent:
            parent = self.requires_tree.get_node(parent)
        for require in requires:
            name, version, _, _, _, _ = load_require(require)
            if parent:
                parent.add_require(name)
            child = self.requires_tree.get_node(name)
            recipe = self.index.get_recipe(name)
            validator_ = get_version_validator(version)
            for version in recipe.versions(validator_):
                if is_child and child.has_version(validator_):
                    continue
                child.add_version(version)
                recipe_dir = recipe.get_recipe_dir(version)
                if recipe_dir in visited:
                    continue
                visited.add(recipe_dir)
                self.collect_all_requires(parent=name, requires=ConanData.get_requires(recipe_dir), visited=visited)

    def download(self, requires):
        print("开始加载依赖关系...")
        self.collect_all_requires(requires, is_child=False)
        self.zip_tool.writestr(REQUIRES_TREE_FILE, self.requires_tree.dumps())
        total = 0
        for node in self.requires_tree.nodes():
            total += len(node.versions)
        with tqdm(total=total, desc="下载并打包资源") as tq:
            for node in self.requires_tree.nodes():
                recipe = self.index.get_recipe(node.name)
                self.downloader.download_versions(recipe, self.zip_tool, node.versions, tq=tq)
                recipe.export_to_zip(self.zip_tool, node.versions, tq=tq)


def extract_file(zip_file, filename, output_dir, out_filename=None):
    if not out_filename:
        out_filename = filename
    output_path = os.path.join(output_dir, out_filename)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    output_path_tmp = output_path + CACHE_TEMPORARY_SUFFIX
    with open(output_path_tmp, "wb") as f:
        with zip_file.open(filename, "r") as zf:
            shutil.copyfileobj(zf, f, length=64 * 1024)
    try:
        os.rename(output_path_tmp, output_path)
    except FileExistsError:
        os.remove(output_path_tmp)
    return filename


class ArtifactoryTool:
    def __init__(self, zip_file_path, server_url, max_workers):
        self.zip_file_path = zip_file_path
        self.pool = ThreadPoolExecutor(max_workers=max_workers)
        if not server_url.endswith("/"):
            self.server_url = server_url + "/"
        else:
            self.server_url = server_url

    def upload(self, zip_file, file, force_upload=False):
        upload_url = self.server_url + remove_prefix(file, SOURCES_PREFIX)
        if not force_upload:
            with requests.get(url=upload_url, stream=True) as rsp:
                if rsp.status_code == 200:
                    return file
        with zip_file.open(file, 'r') as f:
            rsp = requests.put(upload_url, data=f)
            if 300 <= rsp.status_code or rsp.status_code < 200:
                raise Exception(f"Invalid response status code {rsp.status_code}: {rsp.text}")
            return file

    def upload_source_to_server(self, force_upload=False):
        with ZipFile(self.zip_file_path) as zip_file:
            tasks = []
            for file in zip_file.filelist:
                if file.filename.startswith(SOURCES_PREFIX):
                    tasks.append(self.pool.submit(self.upload, zip_file, file.filename, force_upload))
            with tqdm(total=len(tasks), desc="上传source") as tq:
                for task in as_completed(tasks):
                    file = task.result()
                    tq.set_postfix({
                        "uploaded": file
                    })
                    tq.update()

    def extract_recipes(self, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        current_os = platform.system()
        not_linux = current_os != "Linux"
        not_windows = current_os != "Windows"
        with ZipFile(self.zip_file_path) as zip_file:
            def _extract_file(name):
                if name.endswith(CONAN_DATA_YML):
                    with zip_file.open(name) as data_file:
                        conan_data = ConanData(yaml.safe_load(data_file))
                        for source in conan_data.get_sources():
                            url = source.get_url()
                            if url:
                                if url.startswith("http://"):
                                    source.set_url(self.server_url + remove_prefix(url, "http://"))
                                elif url.startswith("https://"):
                                    source.set_url(self.server_url + remove_prefix(url, "https://"))
                        save_path = os.path.join(output_dir, name)
                        os.makedirs(os.path.dirname(save_path), exist_ok=True)
                        with open(save_path, "w", encoding="utf8") as f:
                            f.write(conan_data.dump())
                        return name
                elif name == REQUIRES_TREE_FILE:
                    with zip_file.open(name) as requires_tree_file:
                        requires_tree = RequireTree(yaml.safe_load(requires_tree_file))
                        for node in requires_tree.nodes():
                            node.set_ignore(
                                (
                                        (not_windows and (node.name in WINDOWS_ONLY_PACKAGE))
                                        or
                                        (not_linux and (node.name in LINUX_ONLY_PACKAGE))
                                )
                            )
                        with open(os.path.join(output_dir, REQUIRES_TREE_FILE), "w", encoding="utf8") as f:
                            f.write(requires_tree.dumps())
                        return name
                else:
                    return extract_file(zip_file, name, output_dir)

            tasks = []
            for file in zip_file.filelist:
                if not file.filename.startswith(SOURCES_PREFIX) and not file.is_dir():
                    tasks.append(file.filename)
            with tqdm(total=len(tasks), desc="解包文件") as tq:
                for task in tasks:
                    filename = _extract_file(task)
                    tq.set_postfix({
                        "file": filename
                    })
                    tq.update()


class ConanBuildTool:
    def __init__(self, remote, recipes_dir, max_workers):
        self.remote = remote
        self.index = ConanCenterIndex(recipes_dir)
        self.tree = RequireTree.load(recipes_dir)
        self.pool = ThreadPoolExecutor(max_workers=max_workers)

    def _check_and_remove_exist(self, node, version):
        return node, version, conan_exist_package(f"{node.name}/{version}", self.remote)

    def check_and_clear_exist(self, force=False):
        ignored = set()
        for node in self.tree.nodes():
            if node.ignore:
                ignored.add(node.name)
        for name in ignored:
            self.tree.remove_node(name)
        if not force:
            tasks = []
            for node in self.tree.nodes():
                for version in node.versions:
                    tasks.append(self.pool.submit(self._check_and_remove_exist, node, version))
            with tqdm(total=len(tasks), desc="检查已存在包") as tq:
                for task in tasks:
                    node, version, exist = task.result()
                    if exist:
                        node.remove_version(version)
                    if not node.versions:
                        self.tree.remove_node(node.name)
                    tq.set_postfix({"name": node.name, "version": version}, refresh=False)
                    tq.update()
        total = 0
        for node in self.tree.nodes():
            total += len(node.versions)
        return total

    def get_can_build_node(self):
        for name in FIRST_BUILD_PACKAGE:
            if self.tree.exist_node(name):
                node = self.tree.get_node(name)
                if node.requires:
                    continue
                return node
        nodes = []
        for node in self.tree.nodes():
            if node.requires:
                nodes.append(node)
                continue
            return node
        if nodes:
            raise Exception("can't resolve requires:" + ",".join([node.name for node in nodes]))
        return None

    def create_and_upload(self, force=False, export_only=False):
        total = self.check_and_clear_exist(force)
        with tqdm(total=total, desc="构建依赖") as tq:
            while True:
                node = self.get_can_build_node()
                if not node:
                    break
                node.build_and_upload(self.remote, ConanRecipe(self.index, node.name), tq, export_only=export_only)
                self.tree.remove_node(node.name)


def get_conan_recipes_dir():
    return os.path.join(get_conan_cache_dir(), CONAN_CENTER_RECIPES_DIR)


def get_conan_cache_dir():
    conan_cache_dir = os.getenv("CONAN_CACHE_DIR")
    if not conan_cache_dir:
        raise Exception("环境变量 CONAN_CACHE_DIR 请设置到source文件下载缓存目录路径")
    return conan_cache_dir


def get_conan_local_repository():
    conan_local_repository = os.getenv("CONAN_LOCAL_REPOSITORY")
    if not conan_local_repository:
        raise Exception("环境变量 CONAN_LOCAL_REPOSITORY 请设置到conan本地仓库名称")
    return conan_local_repository


def get_artifactory_upload_server_url():
    artifactory_upload_server_url = os.getenv("ARTIFACTORY_UPLOAD_SERVER_URL")
    if not artifactory_upload_server_url:
        raise Exception("环境变量 ARTIFACTORY_UPLOAD_SERVER_URL 请设置到上传服务器地址")
    return artifactory_upload_server_url


def get_artifactory_download_server_url():
    # 下载服务器地址
    artifactory_download_server_url = os.getenv("ARTIFACTORY_DOWNLOAD_SERVER_URL")
    if not artifactory_download_server_url:
        raise Exception("请设置环境变量 ARTIFACTORY_DOWNLOAD_SERVER_URL 请设置到下载服务器地址")
    return artifactory_download_server_url


def download_conan_index(recipes_dir, index_url):
    if os.path.exists(recipes_dir):
        print(f"正在删除索引目录: {recipes_dir}")
        shutil.rmtree(recipes_dir)
    recipes_dir_tmp = recipes_dir + "_tmp"
    if os.path.exists(recipes_dir_tmp):
        print(f"正在删除临时目录: {recipes_dir_tmp}")
        shutil.rmtree(recipes_dir_tmp)
    os.makedirs(recipes_dir_tmp, exist_ok=True)
    tmp_file = os.path.join(recipes_dir_tmp, "master.zip")
    try:
        print(f"开始下载索引文件: {index_url}")
        with open(tmp_file, "wb") as f:
            download(f, index_url)
        with ZipFile(tmp_file) as zip_file:
            with tqdm(total=len(zip_file.filelist), desc="正在解压索引") as tq:
                for file in zip_file.filelist:
                    if file.filename.startswith(CONAN_CENTER_INDEX_RECIPES_DIR) and not file.is_dir():
                        extract_file(zip_file, file.filename, recipes_dir_tmp,
                                     remove_prefix(file.filename, CONAN_CENTER_INDEX_RECIPES_DIR))
                    tq.update()
    finally:
        try:
            os.remove(tmp_file)
        except FileNotFoundError:
            pass
    os.rename(recipes_dir_tmp, recipes_dir)


def load_requires_from_file(f):
    with open(f, "r", encoding="utf8") as f:
        result = []
        for require in f.readlines():
            require = require.strip()
            if require:
                result.append(require)
        return result


def archive_all_to_file(params):
    """
    下载并导出conan资源到文件
    :param params:
    :return:
    """
    sources_cache_dir = os.path.join(get_conan_cache_dir(), CONAN_CENTER_SOURCE_CACHE_DIR)
    conan_recipes_dir = get_conan_recipes_dir()
    if not os.path.exists(conan_recipes_dir):
        raise Exception(f"索引路径 {conan_recipes_dir} 不存在，请先执行index操作更新索引")
    requires = params.r
    if not requires:
        if not params.f:
            raise Exception("请指定要下载的conan包，或者-f指定包列表文件")
        requires = load_requires_from_file(params.f)
    with ConanRecipeWithRequiresDownloader(conan_recipes_dir, sources_cache_dir,
                                           params.o, params.t, compress_level=params.l) as downloader:
        downloader.download(requires)


def upload_to_artifactory_server(params):
    """
    上传conan资源到服务器
     :param params:
     :return:
    """
    tool = ArtifactoryTool(params.i, get_artifactory_upload_server_url(), max_workers=params.t)
    tool.upload_source_to_server(params.f)


def extract_recipes(params):
    """
    解压conan资源导出包
    """
    if os.path.exists(params.o):
        shutil.rmtree(params.o)
    tool = ArtifactoryTool(params.i, get_artifactory_download_server_url(), params.t)
    tool.extract_recipes(params.o)


def build_and_upload_recipes(params):
    """
    执行构建并上传到指定远程仓库
    """
    tool = ConanBuildTool(get_conan_local_repository(), params.i, params.t)
    tool.create_and_upload(params.f, export_only=params.e)


if __name__ == '__main__':
    _parser = argparse.ArgumentParser(description='Conan中央仓库包离线导出导入工具')
    _parser.set_defaults(func=lambda x: _parser.print_help())
    _subparsers = _parser.add_subparsers(help="子命令")
    _index = _subparsers.add_parser("index", description="更新conan索引缓存")
    _index.add_argument("-r", type=str,
                        default="https://github.com/manx98/conan-center-index/archive/refs/heads/master.zip",
                        help="conan-center-index仓库下载地址")
    _index.set_defaults(func=lambda params: download_conan_index(get_conan_recipes_dir(), params.r))
    _archive = _subparsers.add_parser("arc", description="打包conan资源文件")
    _archive.add_argument("-o", help="保存到的路径")
    _archive.add_argument("-r", nargs='+', help="需要导出的包名")
    _archive.add_argument("-f", type=str, help="需要导出的包清单文件(文件每行一个包名)")
    _archive.add_argument("-l", type=int, default=3, choices=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], help="压缩等级(0-9)")
    _archive.add_argument("-t", type=int, default=cpu_count(), help="最大线程数")
    _archive.set_defaults(func=archive_all_to_file)
    _upload = _subparsers.add_parser("upload", help="上传资源文件到Artifactory服务器")
    _upload.add_argument("-i", required=True, help="需要上传的打包文件路径")
    _upload.add_argument("-f", action='store_true', help="直接覆盖已存在的资源文件")
    _upload.add_argument("-t", type=int, default=cpu_count(), help="最大线程数")
    _upload.set_defaults(func=upload_to_artifactory_server)
    _extract = _subparsers.add_parser("ext", description="解压conan打包文件")
    _extract.add_argument("-i", required=True, help="打包的资源文件")
    _extract.add_argument("-o", required=True, help="解压到的路径")
    _extract.add_argument("-t", type=int, default=cpu_count(), help="最大线程数")
    _extract.set_defaults(func=extract_recipes)
    _build = _subparsers.add_parser("build", description="执行conan构建并上传")
    _build.add_argument("-i", help="解压后的打包文件路径")
    _build.add_argument("-f", action="store_true", help="强制重新构建")
    _build.add_argument("-e", action="store_true", help="仅导出上传")
    _build.add_argument("-t", type=int, default=cpu_count(), help="最大线程数")
    _build.set_defaults(func=build_and_upload_recipes)
    _version = _subparsers.add_parser("version", description="查看当前版本")
    _version.set_defaults(func=lambda params: print(VERSION))
    _args = _parser.parse_args()
    _args.func(_args)