import shutil

import requests
import yaml
from urllib.parse import urlparse
import os
from zipfile import ZipFile
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

THREAD_POOL = ThreadPoolExecutor(max_workers=3)
SOURCES_PREFIX = "_sources/"
REQUIRES_TREE_FILE = "_requires_tree_file.yml"
CONAN_DATA_YML = "conandata.yml"
CONAN_FILE_PY = "conanfile.py"
CONAN_CONFIG_YML = "config.yml"
CACHE_TEMPORARY_SUFFIX = ".tmp"


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
    rsp = requests.get(link, headers=None, stream=True)
    if rsp.status_code == 200:
        # 文件开始下载后，可以按需处理这些数据
        for chunk in rsp.iter_content(chunk_size=64 * 1024):
            if chunk:  # filter out keep-alive new chunks
                file.write(chunk)
    else:
        raise Exception(f"Request failed with status code: {rsp.status_code}")


def conan_exist_package(package, remote):
    return os.popen(f"conan list \"{package}\" -r={remote}").read().find("not found") == -1


class VersionSource:
    def __init__(self, url_data):
        self.data = url_data
        self.url = url_data.get("url")
        if self.url and not isinstance(self.url, (list, tuple)):
            self.set_url([self.url])

    def set_url(self, url):
        self.data["url"] = url
        self.url = url

    def download(self, source_cache_dir):
        if self.url:
            for i, url in enumerate(self.url):
                source_path = get_dir_by_link(source_cache_dir, url)
                if os.path.exists(source_path):
                    return [self, url, source_path]
                source_path_parent = os.path.dirname(source_path)
                os.makedirs(source_path_parent, exist_ok=True)
                source_path_tmp = f"{source_path}{CACHE_TEMPORARY_SUFFIX}"
                try:
                    with open(source_path_tmp, "wb") as file:
                        download(file, url)
                    try:
                        os.rename(source_path_tmp, source_path)
                    except FileExistsError:
                        os.remove(source_path_tmp)
                    return [self, url, source_path]
                except Exception as error:
                    if i != len(self.url) - 1:
                        print(f"Could not download from the URL {url}: {error}.")
                        print("Trying another mirror.")
                    else:
                        raise
        return [self, None, None]

    def apply_to_new_server(self, new_server):
        if self.url:
            if self.url.startswith("http:/"):
                self.set_url(new_server + remove_prefix(self.url, "http:/"))
            elif self.url.startswith("https:/"):
                self.set_url(new_server + remove_prefix(self.url, "https:/"))


class ConanData:
    def __init__(self, conan_data):
        self.data = conan_data
        self.sources = self.load_links()

    def load_links(self):
        result = []
        sources = self.data.get("sources")
        if sources:
            for version in sources.values():
                result.append(VersionSource(version))
        return result

    def dumps(self):
        return yaml.dump(self.data, default_flow_style=False)


class ConanIndex:

    def __init__(self, index_dir, source_cache_dir):
        self.index_dir = index_dir
        self.source_cache_dir = source_cache_dir

    def get_versions(self, name):
        versions = load_yaml(os.path.join(self.index_dir, name, CONAN_CONFIG_YML)).get("versions")
        if versions:
            return list(versions.keys())
        return []

    def export(self, names, save_path):
        requires_set = set(names)
        require_tree = {}
        with ZipFile(save_path, "w") as zip_file:
            while requires_set:
                name = requires_set.pop()
                if name in require_tree:
                    continue
                require_tree_node = []
                require_tree[name] = {
                    "versions": self.get_versions(name),
                    "requires": require_tree_node
                }
                archive_tool = ConanZipExport(index_dir=self.index_dir, name=name, zip_file=zip_file,
                                              source_cache_dir=self.source_cache_dir)
                archive_tool.archive(requires_set=requires_set, require_tree_node=require_tree_node)
            zip_file.writestr(REQUIRES_TREE_FILE, yaml.dump(require_tree).encode())


def collect_requires(collect, conan_file_py_path):
    with open(conan_file_py_path, "r", encoding="utf-8") as f:
        content = f.read()
        start = 0
        start_prefix = "self.requires("
        while True:
            start = content.find(start_prefix, start)
            if start != -1:
                start += len(start_prefix)
                start = content.find('"', start)
                if start != -1:
                    start += 1
                    end = content.find('"', start)
                    if end != -1:
                        name = content[start:end].split("/")
                        if len(name) == 2:
                            collect(name[0])
                        start = end + 1
                        continue
            break


class ConanZipExport:
    def __init__(self, zip_file: ZipFile, index_dir, name, source_cache_dir):
        self.source_cache_dir = source_cache_dir
        self.name = name
        self.recipe_dir = os.path.join(index_dir, name)
        self.zip_file = zip_file

    def archive_conan_source(self, path):
        data = ConanData(load_yaml(path))
        tasks = []
        for link in data.load_links():
            tasks.append(THREAD_POOL.submit(link.download, self.source_cache_dir))
        with tqdm(total=len(tasks), desc=f"下载{self.name}") as tq:
            for task in as_completed(tasks):
                link, url, source_path = task.result()
                if source_path and url:
                    self.zip_file.write(source_path, get_dir_by_link(SOURCES_PREFIX, url))
                tq.set_postfix({"url": url})
                link.set_url(url)
                tq.update()
        return data.dumps()

    def archive(self, requires_set, require_tree_node):
        for root, _, files in os.walk(self.recipe_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arc_file = self.name + remove_prefix(file_path, self.recipe_dir)
                if file == CONAN_DATA_YML:
                    self.zip_file.writestr(arc_file, self.archive_conan_source(file_path))
                else:
                    if file == CONAN_FILE_PY:
                        def collect(name):
                            if name not in require_tree_node:
                                require_tree_node.append(name)
                            requires_set.add(name)

                        collect_requires(collect, file_path)
                    self.zip_file.write(file_path, arc_file)


def extract_file(zip_file, filename, output_dir):
    output_path = os.path.join(output_dir, filename)
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
    def __init__(self, zip_file_path, server_url):
        self.zip_file_path = zip_file_path
        self.server_url = server_url

    def upload(self, zip_file, file):
        upload_url = self.server_url + remove_prefix(file, SOURCES_PREFIX)
        with zip_file.open(file, 'r') as f:
            rsp = requests.put(upload_url, data=f)
            if 300 <= rsp.status_code or rsp.status_code < 200:
                raise Exception(f"Invalid response status code {rsp.status_code}: {rsp.text}")
            return file

    def upload_source_to_server(self):
        with ZipFile(self.zip_file_path) as zip_file:
            tasks = []
            for file in zip_file.filelist:
                if file.filename.startswith(SOURCES_PREFIX):
                    tasks.append(THREAD_POOL.submit(self.upload, zip_file, file.filename))
            with tqdm(total=len(tasks), desc="上传") as tq:
                for task in as_completed(tasks):
                    file = task.result()
                    tq.set_postfix({
                        "uploaded": file
                    })
                    tq.update()

    def extract_recipes(self, output_dir):
        with ZipFile(self.zip_file_path) as zip_file:
            def _extract_file(name):
                if name.endswith(CONAN_DATA_YML):
                    with zip_file.open(name) as data_file:
                        conan_data = ConanData(yaml.safe_load(data_file))
                        for link in conan_data.load_links():
                            if link.url:
                                url = link.url[0]
                                if url.startswith("http:/"):
                                    link.set_url(self.server_url + url.removeprefix("http:/"))
                                elif url.startswith("https:/"):
                                    link.set_url(self.server_url + url.removeprefix("https:/"))
                        save_path = os.path.join(output_dir, name)
                        os.makedirs(os.path.dirname(save_path), exist_ok=True)
                        with open(save_path, "w", encoding="utf8") as f:
                            f.write(conan_data.dumps())
                        return name
                else:
                    return extract_file(zip_file, name, output_dir)

            tasks = []
            for file in zip_file.filelist:
                if not file.filename.startswith(SOURCES_PREFIX):
                    tasks.append(THREAD_POOL.submit(_extract_file, file.filename))
            with tqdm(total=len(tasks), desc="解包文件") as tq:
                for task in as_completed(tasks):
                    filename = task.result()
                    tq.set_postfix({
                        "file": filename
                    })
                    tq.update()


class RequireTree:
    def __init__(self, recipes_dir):
        self.recipes_dir = recipes_dir
        self.require_data = load_yaml(os.path.join(recipes_dir, REQUIRES_TREE_FILE))

    def get_nodes(self):
        nodes = []
        for recipe in self.require_data.keys():
            nodes.append(self.get_node(recipe))
        return nodes

    def get_node(self, recipe):
        node = RequireTreeNode(recipe, self)
        if node.data:
            return node
        return None


class RequireTreeNode:
    def __init__(self, recipe, tree):
        self.tree = tree
        self.recipe = recipe
        self.data = tree.require_data.get(recipe)
        self.recipe_dir = os.path.join(tree.recipes_dir, recipe)
        self.conf = load_yaml(os.path.join(self.recipe_dir, CONAN_CONFIG_YML))
        self.versions = self.data.get("versions")
        if not self.versions:
            self.versions = []
        self.requires = self.data.get("requires")
        if not self.requires:
            self.requires = []

    def exist(self, remote):
        return conan_exist_package(self.recipe, remote)

    def can_build(self, remote):
        for require in self.requires:
            if conan_exist_package(require, remote):
                continue
            return False
        return True

    def remove(self):
        self.tree.require_data.pop(self.recipe)

    def get_work_dir(self, version):
        return os.path.join(self.recipe_dir, self.conf["versions"][version]["folder"])

    def build_and_upload(self, remote):
        with tqdm(total=len(self.versions), desc=f"正在构建 {self.recipe}") as tq:
            for version in self.versions:
                tq.set_postfix({"building": version}, refresh=True)
                code = os.system(
                    f"conan create \"{self.get_work_dir(version)}\" --version={version} --name={self.recipe}")
                if code != 0:
                    raise Exception(f"构建 {self.recipe}/{version} 失败了, 错误退出码:{code}")
                else:
                    tq.set_postfix({"uploading": version}, refresh=True)
                    code = os.system(f"conan upload {self.recipe}/{version} -r={remote}")
                if code != 0:
                    raise Exception(f"执行上传构建 {self.recipe}/{version} 到远程仓库 {remote} 失败, 错误退出码:{code} ")
                tq.update()


class ConanBuildTool:
    def __init__(self, remote, recipes_dir):
        self.remote = remote
        self.recipes_dir = recipes_dir
        self.tree = RequireTree(self.recipes_dir)

    def get_can_build_node(self):
        nodes = self.tree.get_nodes()
        while nodes:
            node = nodes.pop()
            if node.exist(self.remote):
                print(f"[跳过]远程仓库{self.remote}已存在包: {node.recipe}")
                node.remove()
                continue
            if node.can_build(self.remote):
                return node
        nodes = self.tree.get_nodes()
        if nodes:
            raise Exception("can't resolve requires:" + ",".join([node.recipe for node in nodes]))
        return None

    def create_and_upload(self):
        while True:
            node = self.get_can_build_node()
            if not node:
                break
            node.build_and_upload(self.remote)
            node.remove()


# conan-center-index-master\recipes目录
conan_index_recipes_dir = r"E:\code\conan-center-index-master\recipes"
# source文件下载缓存目录
conan_source_cache_dir = r"E:\code\conan-center-index-master\source_cache"
# 上传到的服务器地址
artifactory_server_url = "http://admin:password@192.168.121.140:8081/artifactory/code_source/"
# 本地远程仓库
conan_remote = "conan-hosted"


def archive_conan_artifactory(names, save_path):
    """
    下载并导出conan资源文件
    :param names: 需要导出的包名
    :param save_path: 导出保存到的文件位置
    :return:
    """
    tool = ConanIndex(index_dir=conan_index_recipes_dir, source_cache_dir=conan_source_cache_dir)
    tool.export(names=names, save_path=save_path)


def upload_to_artifactory_server(archive_file_path):
    """
    上传sources到Artifactory服务器
    :param archive_file_path: conan资源导出包
    :return:
    """
    tool = ArtifactoryTool(archive_file_path, artifactory_server_url)
    tool.upload_source_to_server()


def extract_recipes(archive_file_path, output_dir):
    """
    解压conan资源导出包
    :param archive_file_path: conan资源导出包
    :param output_dir: 解压到的路径
    :return:
    """
    tool = ArtifactoryTool(archive_file_path, artifactory_server_url)
    tool.extract_recipes(output_dir)


def build_recipes(recipes_dir):
    """
    执行构建并上传到指定远程仓库
    :param recipes_dir:解压后的打包文件路径
    :return:
    """
    tool = ConanBuildTool(conan_remote, recipes_dir)
    tool.create_and_upload()


archive_conan_artifactory(["zlib"], "./data.zip")
upload_to_artifactory_server("./data.zip")
extract_recipes("./data.zip", "./data")
build_recipes("./data")
