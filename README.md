# 仿真数据管理平台

这是一个用于折叠方向盘结构仿真的数据管理平台原型。

当前仓库先放置展示用原型，重点演示：

- 结构方案演化树
- 同一结构不同方案的模态结果展示
- 方案详情、仿真任务、报告文件关联
- 多方案横向对比

技术栈：

- 前端：单文件 HTML / CSS / JavaScript
- 后端：Python 标准库 HTTP 服务

## 项目结构

- `frontend/`：前端代码，当前入口为 [frontend/index.html](frontend/index.html)。
- `backend/`：后端代码，当前服务入口为 [backend/server.py](backend/server.py)。
- [config.json](config.json)：运行配置，集中设置端口、前端入口、数据目录、附件根路径。
- [run.py](run.py)：项目根目录启动入口。

可以直接用浏览器打开 `frontend/index.html` 查看静态原型；需要保存数据和附件时请通过后端服务访问。

## 开发维护

- [维护指南](docs/维护指南.md)：代码结构、核心状态模型、常见修改入口和验证命令。

## 局域网部署

可以把本项目放在一台工作站上，用 Python 启动内置服务，让局域网用户通过浏览器访问。

启动命令：

```bash
python3 run.py
```

PyCharm 中运行时，选择项目根目录下的 `run.py` 作为脚本即可。如果提示 `Address already in use`，说明端口已经被旧服务占用：

- 旧服务还在用：停止旧的 `python3 run.py` 进程后再运行。
- 临时换端口：在 PyCharm 的 Parameters 中填写 `--port 8089`。
- 固定换端口：修改 `config.json` 里的 `port`。

运行参数默认读取项目根目录的 `config.json`。如果要把运行数据保存到指定磁盘路径，优先修改：

```json
{
  "data_dir": "/path/to/server_data"
}
```

也可以临时覆盖配置：

```bash
python3 run.py --data-dir /path/to/server_data
```

启动后终端会显示两个地址：

- `Local`：工作站本机访问地址
- `LAN`：局域网其他电脑访问地址，例如 `http://192.168.1.20:8088/`

首次运行会自动创建默认管理员账号：

```text
账号：admin
密码：admin123
角色：管理员
```

数据保存位置：

- 平台数据：`server_data/state.json`
- 用户账号：`server_data/users.json`
- 附件文件：默认保存在项目目录下的 `server_data/attachments/`
- 自定义运行数据路径：保存在 `config.json` 的 `data_dir` 指定目录下

说明：

- 通过 `http://工作站IP:8088/` 访问时，系统会自动使用工作站统一数据和附件目录。
- 附件会按 `server_data/attachments/零件类型/零件编号_零件名称/卡片编号_卡片名称_卡片ID/` 分层保存，卡片详情快照写入该卡片目录的 `card_detail.json`。
- 直接双击 `frontend/index.html` 打开时，仍是单机浏览器本地存储模式。
- 如果其他电脑无法访问，请检查工作站防火墙是否允许 `8088` 端口入站访问。
