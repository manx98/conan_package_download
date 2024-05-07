# Conan包离线导入导出工具
## 环境变量配置
```text
# Artifactory服务器Generic源码下载地址
ARTIFACTORY_DOWNLOAD_SERVER_URL=http://admin:password@127.0.0.1:8081/artifactory/code_source/
# Artifactory服务器GenericGeneric源码上传地址
ARTIFACTORY_UPLOAD_SERVER_URL=http://admin:password@127.0.0.1:8081/artifactory/code_source/
# 离线缓存目录
CONAN_CACHE_DIR=E:\code\conan-center-index-master
# 上传配方到的远程仓库名称
CONAN_LOCAL_REPOSITORY=conan-hosted
```
## 使用教程
```text
index 更新conan索引缓存
  -r conan-center-index仓库下载地址(默认 https://github.com/manx98/conan-center-index/archive/refs/heads/master.zip)
arc 离线打包conan资源文件
  -o 路径: 保存到的路径
  -r 依赖包1 依赖包2: 需要导出的包名(可以指定多个)
  -f 需要导出的包清单文件(-r, -f 二选一)
  -l 压缩等级(默认9)
  -t 最大线程数
upload 上传资源文件到Artifactory服务器
  -i 路径: 需要上传的打包文件路径
  -f 直接覆盖已存在的资源文件
ext 解压conan打包文件
  -i 路径: 打包的资源文件
  -o 路径: 解压到的路径
  -t 最大线程数
build 执行conan构建并上传
  -i 路径: 解压后的打包文件路径
  -f 强制重新构建
  -e 仅导出上传
  -t 最大线程数
version 查看当前版本
```