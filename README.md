# Linux SSH 管理项目

基于 Python 的 SSH 运维管理库：封装 `paramiko` 完成 SSH 连接 / 命令 / SFTP 文件操作，并用 SQLite 管理主机与主机组。

## 文件说明



| 文件                 | 说明                                          |
| ------------------ | ------------------------------------------- |
| `MySSHBase.py`     | SSH/SFTP 封装库（连接、命令执行、安全过滤、交互命令、远程文件读写、上传下载、目录操作） |
| `SSHSessionManager.py` | 多 SSH 会话管理器（类 tmux：多窗体创建、遍历、切入切出、自由读写执行） |
| `AdminDataBase.py` | SQLite 主机 / 主机组管理库（增删改查、分组、连通性检测、备份恢复、导入导出） |
| `mcp.py`           | MCP 服务器：把全部能力封装为 78 个 MCP 工具（host_*/group_*/db_*/ssh_*/session_*），供支持 MCP 的 AI 客户端调用 |
| `demo.py`          | 全功能演示 + 冒烟测试脚本（可对接真实主机回归验证）                 |
| `requirements.txt` | 依赖清单（`paramiko>=3.0`、`mcp>=2.0`）                       |

## 安装依赖



```
pip install -r requirements.txt
```

## MySSHBase 用法

所有方法返回 **JSON 字符串**，用 `json.loads()` 解析即可。



```
from MySSHBase import MySSH

ssh = MySSH("8.140.234.178", "root", "密码", default\_port=22)

\# 方式一：手动管理

ssh.Init()

print(ssh.BatchCMD("uname -a"))

ssh.CloseSSH()

\# 方式二：with 上下文（自动连接、退出自动关闭）

with MySSH("8.140.234.178", "root", "密码", 22) as ssh:

&#x20;   print(ssh.GetSystemVersion())
```

### 方法一览



| 方法                                           | 说明                                |
| -------------------------------------------- | --------------------------------- |
| `Init()`                                     | 初始化 SSH 连接                        |
| `IsConnected()`                              | 检测连接是否有效                          |
| `CloseSSH()`                                 | 关闭连接（幂等）                          |
| `BatchCMD(cmd, timeout=None, get_pty=False)` | 执行命令，返回 stdout/stderr/ 退出码 / 是否超时 |
| `BatchCMD_NotRef(cmd, timeout=None)`         | 执行非交互命令，只看是否执行完                   |
| `InvokeCMD(cmd, get_pty=True)`               | 启动**手动交互会话**，返回 `SSHInteractiveSession` 对象 |
| `InteractiveCMD(cmd, interactions=None)`     | **脚本化交互命令**：按提示列表自动等待并应答          |
| `SetCommandFilter(mode, blacklist, whitelist)` | 设置命令安全过滤（off / blacklist / whitelist）  |
| `AddBlacklist(*rules)` / `AddWhitelist(*rules)` | 追加过滤规则                              |
| `GetCommandFilter()`                        | 查看当前过滤策略与规则列表                     |
| `GetSystemVersion()`                         | 获取 `uname -a`                     |
| `read_remote_file(path)`                     | 读取远程文件全文                          |
| `write_remote_file(path, content, mode="w")` | 写入 / 追加远程文件（w/a/wb/ab）            |
| `GetRemoteFile(remote, local)`               | 下载远程文件到本地                         |
| `PutLocalFile(local, remote)`                | 上传本地文件到远程                         |
| `ListRemoteDir(path=".")`                    | 列远程目录                             |
| `MakeRemoteDir(path)`                        | 远程创建目录                            |
| `RemoveRemoteFile(path)`                     | 删除远程文件                            |
| `RemoveRemoteDir(path)`                      | 删除远程空目录                           |
| `StatRemoteFile(path)`                       | 查看远程文件 / 目录属性                     |

### BatchCMD 返回格式



```
{

&#x20; "code": 200,

&#x20; "msg": "命令执行成功，已获取结果",

&#x20; "data": "Linux ...",

&#x20; "stderr": "",

&#x20; "exit\_status": 0,

&#x20; "timed\_out": false

}
```

> 说明：
>
> `code=200`
>
>  表示调用成功；命令是否真正成功看 
>
> `exit_status`
>
> 。命令无 stdout 但退出码为 0 时也返回 200（原版本会误判为失败）。

### 交互式命令支持

适用于 `sudo` 密码提示、确认询问、菜单选择等需要"看到提示 → 输入应答"的场景。两种方式：

**方式一：脚本化自动应答（`InteractiveCMD`）**——按顺序等待提示并发送应答，超时自动保护：

```
\# sudo 需要密码 + 确认 的场景

res = ssh.InteractiveCMD("sudo systemctl restart nginx", [

&#x20;   {"expect": r"\[sudo\].*password", "send": "我的密码"},

&#x20;   {"expect": "Do you want to continue", "send": "y"},

])

print(res)   # 含完整输出、exit_status、每一步 matched 结果
```

**方式二：手动交互会话（`InvokeCMD`）**——返回会话对象，随时读输出、发输入、等提示：

```
session = ssh.InvokeCMD("sudo apt update")

session.expect(r"password.*:", timeout=10)   # 等待密码提示

session.send("我的密码")                      # 发密码并回车

exit_code = session.wait_exit()              # 等命令结束

print(session.get_output())                  # 拿完整输出

session.close()
```

`SSHInteractiveSession` 常用方法：`read(timeout)` 读新输出、`send(text)` 发输入、`send_eof()` 发 EOF、`expect(pattern, timeout)` 等提示（正则）、`wait_exit(timeout)` 拿退出码、`get_output()` 取累计输出、`close()` 关闭。

### 命令安全过滤（黑名单 / 白名单）

在执行命令前先按策略检查，**被拦截的命令不会发送到远端**，返回 `code=403`。三种模式：`off`（默认，不过滤）、`blacklist`（黑名单）、`whitelist`（白名单）。

**黑名单模式**——拦截规则命中的命令，其余放行。默认内置 20 条规则：删除类（`rm`/`rmdir`/`del`/`rd`/`format`）、磁盘分区格式化（`dd`/`mkfs`/`fdisk`/`parted` 等）、关机重启（`shutdown`/`reboot`/`halt`/`poweroff`/`init 0/6`）、高危操作（`kill -9 -1`、fork 炸弹、直接写 `/dev/sd*`）。

**白名单模式**——只放行名单内命令（默认 83 个常用命令），其余一律拦截。适合对执行面有严格要求的场景。

```
\# 方式一：构造时开启（推荐）

ssh = MySSH("8.140.234.178", "root", "密码", 22, filter_mode="blacklist")

\# 方式二：运行时切换

ssh.SetCommandFilter("blacklist")

print(ssh.BatchCMD("rm -rf /tmp/x"))        # code=403，未执行

ssh.AddBlacklist("poweroff")                 # 追加自定义规则

ssh.SetCommandFilter("whitelist")            # 切换白名单

print(ssh.BatchCMD("ls /tmp"))               # 放行

print(ssh.BatchCMD("fdisk -l"))              # code=403，不在名单
```

规则说明：

* 纯命令名（如 `"rm"`）自动按「段首命令」匹配，可带 `sudo` 前缀；管道 / `&&` / `;` 串联命令**逐段检查**，任何一段命中即拦截。
* 含正则元字符的条目（如 `r"init\s+[06]\b"`）原样作为正则全文匹配，可自定义复杂规则。
* 拦截返回 `{"code": 403, "msg": "命令被安全策略拦截…", ...}`；`InvokeCMD` 会抛 `CommandFilterError` 异常。
* 过滤对所有执行入口生效：`BatchCMD`、`BatchCMD_NotRef`、`InteractiveCMD`、`InvokeCMD`。

> 定位说明：本过滤是「防手滑」级保护，能拦截直接的危险命令，但**不是**强安全边界——`bash -c 'rm -rf /'` 这类间接执行需靠系统层（sudoers / auditd / 跳板机）约束。

### 多会话管理（SSHSessionManager，类 tmux）

同时维护多个 SSH 交互式 shell 窗体，可遍历 / 统计、切入切出（焦点切换）、对任意窗体自由读写与执行命令。

```
from SSHSessionManager import SSHSessionManager

mgr = SSHSessionManager()

\# 新建窗体（不传 ssh 时内部新建连接；传已连接 MySSH 实例则复用连接）

r1 = mgr.CreateSession("8.140.234.178", "root", "密码", 22, name="窗口A", ssh=ssh)

r2 = mgr.CreateSession("8.140.234.178", "root", "密码", 22, name="窗口B")

mgr.ListSessions()                # 遍历：数量、激活窗体、每个窗体的信息与最新输出

mgr.SwitchSession("S2")           # 切入窗口B（窗口A切出后台保留）

mgr.Execute("S1", "echo hi")      # 指定窗体执行命令（默认执行在激活窗体）

mgr.Send("S2", "echo test")       # 向任意窗体发送输入

mgr.Read("S1", timeout=1)         # 读取任意窗体新输出

mgr.WaitPrompt("S2", pattern=r"#\s*$")   # 等待提示符，确认命令执行完毕

mgr.CloseSession("S2")            # 关闭单个窗体

mgr.CloseAll()                    # 全部关闭（新建的连接随之释放）
```

方法一览：`CreateSession`、`ListSessions`、`GetSessionCount`、`GetSessionInfo`、`SwitchSession`、`GetActiveSession`、`Read`、`Send`、`Execute`、`WaitPrompt`、`GetCommandState`、`WaitCommandFinish`、`CloseSession`、`CloseAll`、`GetSession`（返回底层会话对象供高级用法）。

说明：会话输出为真实终端内容（含 ANSI 控制符，如光标/括号粘贴码），这是终端原样语义；输出累计保存在各自缓冲区，`Read`/`Execute` 返回本次新增文本的同时附带完整 `buffer`。

**切出后命令继续执行**：每个窗体是独立的持久 SSH 通道，命令运行在**远端服务器**上，本地切换焦点（`SwitchSession`）不影响远端进程——与 tmux 切窗一致。管理器内置**后台收取线程**（创建会话时自动启动，`StartDrainer`/`StopDrainer` 可手动控制），实时把各窗体输出读入各自缓冲区：即使窗体被切出，其输出也在持续收集中，切回即可看到完整结果；长任务大量输出也不会因无人读取而触发 TCP 背压阻塞。示例：

```
mgr.Execute("S3", "sleep 3; echo 后台任务已完成", wait=1)  # 启动长任务
mgr.SwitchSession("S4")                                   # 立即切出
# ... 在 S4 正常干活 ...
mgr.SwitchSession("S3")                                   # 切回
mgr.Read("S3")   # 输出已在缓冲区："... 后台任务已完成 [root@... ~]#"
```

**命令执行状态检测**（判断窗体空闲 / 正在执行 / 已关闭）：

```
mgr.GetCommandState("S3")
\# -> {"state": "idle"}  空闲（提示符就绪）
\# -> {"state": "busy"}  正在执行（探针未回显）
\# -> {"state": "closed"} 已关闭

mgr.WaitCommandFinish("S3", timeout=30)   # 阻塞等待当前命令执行完毕
```

检测原理（两级）：

1. **提示符检测**（无副作用）：输出尾部匹配提示符（默认 `#` 或 `$` 结尾）→ 空闲；
2. **探针检测**：否则发送无害标记 `echo __STxxx__`，短时间出现**独立成行的标记**（命令真正执行后的输出，而非终端回显）→ 空闲；未出现 → 探针排在当前命令之后，说明命令正在执行。

可配参数：`GetCommandState(timeout=2, use_probe=True, prompt_pattern=r"[#$]\s*$")`；无提示符的终端可设 `use_probe=False` 直接按 busy 处理。`WaitCommandFinish` 仅用提示符检测等待，不产生探针噪声。

**会话间互发消息**：

```
mgr.PostMessage("S3", "你好，窗口C", from_id="窗口D")   # 投递到收件箱（不干扰终端输入）
mgr.Broadcast("系统通知：稍后维护")                      # 广播给所有会话
mgr.ReadMessages("S3")                                 # 读取（默认读后清空）收件箱
mgr.SendToSession("S4", "echo hi", from_session_id="S3")  # 直接注入输入到另一窗体并回车执行
```

**会话持久化（配置 + 命令历史，重启后恢复）**：

```
mgr.ExportSessions("backup.json")    # 导出所有会话的连接配置 + 命令历史
mgr.CloseAll()                        # 模拟重启/退出
mgr.RestoreSessions("backup.json")   # 重新连接并恢复（名称与历史保留）
mgr.RestoreSessions("backup.json", connect=False)   # dry-run，只解析校验不连接
mgr.GetHistory("S4")                  # 查看任意会话的命令历史
```

> 注意：`ExportSessions` 默认 `include_password=True`，备份文件含明文密码，请妥善保管；不需要时可传 `include_password=False`。

## AdminDataBase 用法

所有方法返回 **dict**：`{"code": 1/0, "msg": "...", "data": ...}`。



```
from AdminDataBase import AdminDataBase

db = AdminDataBase("hosts.db")

db.InitDatabase()

db.AddHost(address="8.140.234.178", username="root", password="密码", port="22")

print(db.ShowHostList())

db.AddHostGroup("WebServers")

db.AddHostGroupOnUUID("WebServers", "\<uuid>")

print(db.PingAllHosts())   # 真实 TCP 连通检测
```

### 方法一览



| 类别    | 方法                                                                                                                                                                                                                                                                      |
| ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 初始化   | `InitDatabase()`                                                                                                                                                                                                                                                        |
| 主机增删改 | `AddHost()`（uuid 可空，自动生成）、`ModifyHost()`、`ModifyHostIp()`、`ModifyHostUserPwd()`、`ModifyHostPort()`、`DeleteHost()`、`BatchDeleteHost()`                                                                                                                                   |
| 主机查询  | `ShowHostList()`、`SearchHostByUUID()`、`SearchHostByAddress()`、`FuzzySearchHost()`、`CheckHostExist()`、`GetHostCount()`                                                                                                                                                   |
| 分组管理  | `AddHostGroup()`、`DeleteHostGroup()`、`RenameHostGroup()`、`AddHostGroupOnUUID()`、`DeleteHostGroupOnUUID()`、`BatchAddHost2Group()`、`BatchDelHostFromGroup()`、`ClearGroupHosts()`、`ShowAllGroup()`、`ShowGroup()`、`ShowSingleGroup()`、`CheckGroupExist()`、`GetGroupCount()` |
| 连通检测  | `PingGroup()`、`PingAllHosts()`（真实 TCP 连接目标端口并测延迟）                                                                                                                                                                                                                       |
| 备份恢复  | `BackupDatabase()`、`RestoreDatabase()`                                                                                                                                                                                                                                  |
| 导入导出  | `ExportHosts2CSV()`、`ExportHosts2JSON()`、`ImportHostsFromCSV()`                                                                                                                                                                                                         |
| 清空    | `ClearAllHosts()`、`ClearAllGroups()`                                                                                                                                                                                                                                    |

## MCP 服务器（mcp.py）

把项目全部能力封装为 **78 个 MCP 工具**，供支持 MCP 的 AI 客户端（Claude / 豆包等）以「对话」方式执行 Linux 运维任务。工具按前缀分三组：

| 前缀 | 数量 | 覆盖能力 |
| --- | --- | --- |
| `host_*` / `group_*` / `db_*` | 36 | 主机增删改查、搜索、统计、主机组全套、TCP 连通检测、备份恢复、CSV/JSON 导入导出、清空 |
| `ssh_*` | 18 | 命令执行（`ssh_cmd`）、非交互（`ssh_cmd_not_ref`）、交互应答（`ssh_interactive_cmd`）、安全过滤策略（`ssh_filter_*`）、SFTP 文件全套、系统版本 |
| `session_*` | 24 | 多会话窗体：创建/遍历/切入切出/读写执行、命令状态检测（`session_state`/`session_wait_finish`）、会话间消息、历史与持久化 |

### 运行

```
pip install -r requirements.txt
python mcp.py                      # 默认 streamable-http，监听 127.0.0.1:8001
python mcp.py --port 9001 --log-level DEBUG
python mcp.py --config config.json # 可选配置文件覆盖默认值
python mcp.py --list-tools         # 仅打印已注册工具清单后退出
```

配置项（`config.json` 可覆盖）：`mcp_host`、`mcp_port`、`mcp_transport`、`log_level`、`database_path`、`default_filter_mode`（默认 `blacklist`）、`system_prompt`。

### 设计要点

* **安全过滤默认开启**：`default_filter_mode="blacklist"`，AI 执行的 `rm`/`reboot` 等危险命令会被拦截（返回 `code=403`），可用 `ssh_filter_set` / `ssh_filter_add_blacklist` / `ssh_filter_add_whitelist` 调整（进程级，作用于后续所有调用）。
* **交互式命令**：`ssh_interactive_cmd(uuid, command, interactions_json)`，`interactions_json` 传 `[{"expect": "password", "send": "xxx"}]` 一次调用完成 sudo 密码/确认询问/菜单选择。
* **无状态 vs 有状态**：`ssh_*` 每次调用独立建连、用完即关（无状态）；`session_*` 的连接由管理器持有，跨工具调用持续存在（可切出后继续跑命令，再用 `session_state`/`session_wait_finish` 判断状态）。
* **同名遮蔽自愈**：本文件与官方 `mcp` 包同名，运行时自动在 `sys.path` 中让位，保证导入官方 SDK（`mcp.server.mcpserver.MCPServer`，mcp>=2.0）。
* **不阻塞事件循环**：所有同步 SSH/SQLite 调用均放入线程池（`asyncio.to_thread`），长命令不会卡住整个服务。
* **凭据提示**：密码以明文出现在工具参数与配置中，请仅在可信网络使用。