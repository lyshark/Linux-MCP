import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

# ---------- 同名遮蔽自愈：先让出项目目录，保证导入官方 mcp 包 ----------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR in sys.path:
    sys.path.remove(_SCRIPT_DIR)
from mcp.server.mcpserver import MCPServer  # noqa: E402
sys.path.insert(0, _SCRIPT_DIR)

from AdminDataBase import AdminDataBase  # noqa: E402
from MySSHBase import MySSH  # noqa: E402
from SSHSessionManager import SSHSessionManager  # noqa: E402

# ---------- 日志 ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(_SCRIPT_DIR, "server.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ---------- 配置 ----------

class ServerConfig:
    """服务配置：默认值 + 可从 JSON 配置文件覆盖"""

    def __init__(self):
        self.mcp_service_id = "aissh_mcp_server_cherry"
        self.mcp_host = "127.0.0.1"
        self.mcp_port = 8001
        self.mcp_transport = "streamable-http"
        self.log_level = "INFO"
        self.timeout = 1800
        self.database_path = "admin.db"
        # 命令安全过滤默认模式：off / blacklist / whitelist（默认黑名单防手滑）
        self.default_filter_mode = "blacklist"
        self.system_prompt = """
你是 Linux SSH 运维助手，通过 MCP 工具连接远程 Linux 服务器执行运维任务。

规范：
0. 请全程使用简体中文回复。
1. 请自主决策、迭代执行完成用户任务；每次调用命令前，先用一句话说明这一步在做什么。
2. 命令安全过滤默认开启（黑名单）：rm / rmdir / del / dd / mkfs / reboot / shutdown
   等危险命令会被拦截（结果 code=403）。这是保护机制：被拦截后请改用更安全的等价操作，
   或与用户确认必要性后，再通过 ssh_filter_set / ssh_filter_add_blacklist 调整策略。
3. 交互式命令（sudo 密码、确认询问、菜单选择）请使用 ssh_interactive_cmd，
   在 interactions_json 参数中按顺序给出 {"expect": 提示, "send": 应答}，一次调用完成。
4. 多会话场景使用 session_* 工具：可创建多个窗体、自由切入切出（切出后命令继续执行）、
   用 session_state / session_wait_finish 判断命令执行状态，切回后读输出。
5. 当命令输出数据量过大时，仅返回执行状态（成功/失败），不返回具体内容。
6. 主机信息可从数据库查询（host_list / host_search_address 等）后按 uuid 操作；
   也可用 session_create 直接连接任意主机。

安全原则：仅执行用户授权的运维操作；删除、格式化、关机等高危操作先与用户确认必要性。
"""

    def load_from_file(self, config_path: str) -> None:
        """从 JSON 配置文件加载配置（仅覆盖已存在的属性）"""
        if not os.path.exists(config_path):
            logger.warning("配置文件 %s 不存在，将使用默认配置", config_path)
            return
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            for key, value in config_data.items():
                if hasattr(self, key):
                    setattr(self, key, value)
                    logger.info("从配置文件加载 %s: %s", key, value)
        except Exception as e:
            logger.error("加载配置文件失败: %s，将使用默认配置", e)


# ---------- 返回格式化 ----------

class ResponseFormatter:
    @staticmethod
    def success(result: Any) -> str:
        return json.dumps({"status": "success", "result": result},
                          ensure_ascii=False, indent=2)

    @staticmethod
    def error(message: str, details: Optional[Any] = None) -> str:
        response = {
            "status": "error",
            "message": f"{message}（详情：{details}）" if details else message,
        }
        return json.dumps(response, ensure_ascii=False, indent=2)


class ToolError(Exception):
    """业务错误：已知失败原因，返回 error 时不含堆栈"""
    pass


# ---------- 数据库工具（AdminDataBase） ----------

class DatabaseTools:
    """AdminDataBase 全部能力 → MCP 工具（host_* / group_* / db_*）"""

    def __init__(self, config: ServerConfig):
        self.db = AdminDataBase(config.database_path)
        init = self.db.InitDatabase()
        if init["code"] == 1:
            logger.info(init["msg"])
        else:
            logger.warning(init["msg"])

    @staticmethod
    async def _call(func, *args):
        """同步数据库调用放入线程池，避免阻塞事件循环"""
        return await asyncio.to_thread(func, *args)

    async def _wrap(self, action: str, func, *args) -> str:
        try:
            result = await self._call(func, *args)
            return ResponseFormatter.success(result)
        except Exception as e:
            logger.exception("%s异常", action)
            return ResponseFormatter.error(action, e)

    # ---- 初始化 / 主机 ----

    async def db_init(self) -> str:
        """初始化数据库（建表 + 默认数据）"""
        return await self._wrap("初始化数据库失败", self.db.InitDatabase)

    async def host_list(self) -> str:
        """显示所有主机列表"""
        return await self._wrap("显示主机列表失败", self.db.ShowHostList)

    async def host_add(self, address: str, username: str, password: str,
                       port: str = "22", uuid: Optional[str] = None) -> str:
        """添加新主机（uuid 留空自动生成；地址/用户必填，端口自动校验）"""
        return await self._wrap("添加主机失败", self.db.AddHost, uuid, address,
                                username, password, port)

    async def host_modify(self, uuid: str, address: str, username: str,
                          password: str, port: str) -> str:
        """全量修改主机信息（端口自动校验）"""
        return await self._wrap("修改主机失败", self.db.ModifyHost, uuid, address,
                                username, password, port)

    async def host_modify_ip(self, uuid: str, new_address: str) -> str:
        """单独修改主机 IP 地址"""
        return await self._wrap("修改主机IP失败", self.db.ModifyHostIp, uuid, new_address)

    async def host_modify_user_pwd(self, uuid: str, new_username: str, new_password: str) -> str:
        """单独修改主机账号和密码"""
        return await self._wrap("修改主机账号密码失败", self.db.ModifyHostUserPwd,
                                uuid, new_username, new_password)

    async def host_modify_port(self, uuid: str, new_port: str) -> str:
        """单独修改主机端口号"""
        return await self._wrap("修改主机端口失败", self.db.ModifyHostPort, uuid, new_port)

    async def host_delete(self, uuid: str) -> str:
        """删除指定主机（组内关联自动级联删除）"""
        return await self._wrap("删除主机失败", self.db.DeleteHost, uuid)

    async def host_batch_delete(self, uuid_list: List[str]) -> str:
        """批量删除主机（uuid_list 为 UUID 数组）"""
        return await self._wrap("批量删除主机失败", self.db.BatchDeleteHost, uuid_list)

    # ---- 主机查询 ----

    async def host_search_uuid(self, uuid: str) -> str:
        """按 UUID 精准查询主机"""
        return await self._wrap("查询主机失败", self.db.SearchHostByUUID, uuid)

    async def host_search_address(self, address: str) -> str:
        """按地址精准查询主机"""
        return await self._wrap("查询主机失败", self.db.SearchHostByAddress, address)

    async def host_fuzzy_search(self, keyword: str) -> str:
        """按关键词模糊查询主机（匹配地址或用户名）"""
        return await self._wrap("模糊查询主机失败", self.db.FuzzySearchHost, keyword)

    async def host_check(self, uuid: str) -> str:
        """校验主机是否存在"""
        return await self._wrap("校验主机失败", self.db.CheckHostExist, uuid)

    async def host_count(self) -> str:
        """统计主机数量"""
        return await self._wrap("统计主机数量失败", self.db.GetHostCount)

    async def group_count(self) -> str:
        """统计主机组数量"""
        return await self._wrap("统计主机组数量失败", self.db.GetGroupCount)

    async def group_check(self, group_name: str) -> str:
        """校验主机组是否存在"""
        return await self._wrap("校验主机组失败", self.db.CheckGroupExist, group_name)

    # ---- 主机组管理 ----

    async def group_add(self, add_group_name: str) -> str:
        """添加主机组"""
        return await self._wrap("添加主机组失败", self.db.AddHostGroup, add_group_name)

    async def group_delete(self, delete_group_name: str) -> str:
        """删除主机组（组内关联自动清理）"""
        return await self._wrap("删除主机组失败", self.db.DeleteHostGroup, delete_group_name)

    async def group_rename(self, old_name: str, new_name: str) -> str:
        """重命名主机组（外键安全）"""
        return await self._wrap("重命名主机组失败", self.db.RenameHostGroup, old_name, new_name)

    async def group_add_host(self, group_name: str, uuid: str) -> str:
        """向指定主机组添加一台主机（按 UUID）"""
        return await self._wrap("向组添加主机失败", self.db.AddHostGroupOnUUID, group_name, uuid)

    async def group_remove_host(self, group_name: str, uuid: str) -> str:
        """从指定主机组移除一台主机（按 UUID）"""
        return await self._wrap("从组移除主机失败", self.db.DeleteHostGroupOnUUID, group_name, uuid)

    async def group_batch_add(self, group_name: str, uuid_list: List[str]) -> str:
        """批量向主机组添加主机（uuid_list 为 UUID 数组）"""
        return await self._wrap("批量向组添加主机失败", self.db.BatchAddHost2Group, group_name, uuid_list)

    async def group_batch_remove(self, group_name: str, uuid_list: List[str]) -> str:
        """批量从主机组移除主机"""
        return await self._wrap("批量从组移除主机失败", self.db.BatchDelHostFromGroup, group_name, uuid_list)

    async def group_clear(self, group_name: str) -> str:
        """清空组内所有主机（组本身保留）"""
        return await self._wrap("清空组内主机失败", self.db.ClearGroupHosts, group_name)

    async def group_show_all(self) -> str:
        """显示所有组及组内主机详情"""
        return await self._wrap("显示所有组失败", self.db.ShowAllGroup)

    async def group_show(self) -> str:
        """显示所有组（含主机数与 UUID 列表）"""
        return await self._wrap("显示组概览失败", self.db.ShowGroup)

    async def group_show_single(self, group_name: str) -> str:
        """查询单个主机组详情"""
        return await self._wrap("查询组详情失败", self.db.ShowSingleGroup, group_name)

    # ---- 连通性 / 备份 / 导入导出 ----

    async def ping_group(self, group_name: str) -> str:
        """组内主机真实 TCP 连通检测（测目标端口与延迟）"""
        return await self._wrap("组内Ping失败", self.db.PingGroup, group_name)

    async def ping_all(self) -> str:
        """全部主机真实 TCP 连通检测"""
        return await self._wrap("全局Ping失败", self.db.PingAllHosts)

    async def db_backup(self, backup_path: Optional[str] = None) -> str:
        """备份数据库为 SQL 文件（不传路径时自动按时间戳命名）"""
        return await self._wrap("备份数据库失败", self.db.BackupDatabase, backup_path)

    async def db_restore(self, sql_path: str) -> str:
        """从备份 SQL 文件恢复数据库"""
        return await self._wrap("恢复数据库失败", self.db.RestoreDatabase, sql_path)

    async def db_export_csv(self, csv_path: str = "hosts_export.csv") -> str:
        """导出全部主机到 CSV（UTF-8 BOM，Excel 可直接打开）"""
        return await self._wrap("导出CSV失败", self.db.ExportHosts2CSV, csv_path)

    async def db_export_json(self, json_path: str = "hosts_export.json") -> str:
        """导出全部主机到 JSON"""
        return await self._wrap("导出JSON失败", self.db.ExportHosts2JSON, json_path)

    async def db_import_csv(self, csv_path: str) -> str:
        """从 CSV 导入主机（列头兼容导出格式）"""
        return await self._wrap("导入CSV失败", self.db.ImportHostsFromCSV, csv_path)

    async def db_clear_hosts(self) -> str:
        """清空所有主机（主机组保留）"""
        return await self._wrap("清空主机失败", self.db.ClearAllHosts)

    async def db_clear_groups(self) -> str:
        """清空所有主机组（主机保留）"""
        return await self._wrap("清空主机组失败", self.db.ClearAllGroups)


# ---------- 单机 SSH 工具（MySSHBase） ----------

class SSHTools:
    """MySSHBase 单机能力 → MCP 工具（ssh_*）
    无状态模型：每次调用按 UUID 查主机 → 独立建连 → 用完自动关闭。
    安全过滤配置为进程级状态，可被 ssh_filter_* 工具调整并作用于后续所有调用。
    """

    def __init__(self, config: ServerConfig):
        self.config = config
        self.db = AdminDataBase(config.database_path)
        self.filter_mode = config.default_filter_mode
        self._extra_blacklist: List[str] = []
        self._extra_whitelist: List[str] = []

    def _find_host(self, uuid: str) -> Dict[str, str]:
        res = self.db.ShowHostList()
        if res["code"] != 1:
            raise ToolError(f"查询主机列表失败：{res['msg']}")
        for host in res["data"]:
            if host["uuid"] == uuid:
                return host
        raise ToolError(f"UUID {uuid} 对应的主机不存在")

    @asynccontextmanager
    async def _connect(self, uuid: str):
        """查主机 → 建连（应用安全过滤）→ yield ssh → 自动关闭"""
        host = await asyncio.to_thread(self._find_host, uuid)
        ssh = MySSH(
            address=host["address"],
            username=host["username"],
            password=host["password"],
            default_port=int(host["port"]),
            filter_mode=self.filter_mode,
        )
        for rule in self._extra_blacklist:
            ssh.AddBlacklist(rule)
        for rule in self._extra_whitelist:
            ssh.AddWhitelist(rule)
        init = json.loads(await asyncio.to_thread(ssh.Init))
        if init["code"] != 200:
            raise ToolError(f"SSH连接失败：{init['msg']}")
        try:
            yield ssh
        finally:
            await asyncio.to_thread(ssh.CloseSSH)

    async def _run(self, action: str, fn, *args) -> str:
        """在独立连接中执行同步函数并统一格式化结果"""
        try:
            async with self._connect(args[0]) as ssh:
                result = json.loads(await asyncio.to_thread(fn, ssh, *args[1:]))
            return ResponseFormatter.success(result)
        except ToolError as e:
            return ResponseFormatter.error(str(e))
        except Exception as e:
            logger.exception("%s异常", action)
            return ResponseFormatter.error(action, e)

    @staticmethod
    def _parse_interactions(interactions_json: str) -> List[Dict[str, Any]]:
        """解析交互应答列表 JSON → [{"expect","send","timeout"}]"""
        if not interactions_json or not interactions_json.strip():
            return []
        try:
            data = json.loads(interactions_json)
        except json.JSONDecodeError as e:
            raise ToolError(f"interactions_json 不是合法 JSON：{e}")
        if not isinstance(data, list):
            raise ToolError("interactions_json 必须是 JSON 数组，如 [{\"expect\": \"password\", \"send\": \"xxx\"}]")
        return data

    # ---- 命令执行 ----

    async def ssh_cmd(self, uuid: str, command: str, timeout: Optional[int] = None) -> str:
        """在指定主机执行 Linux 命令（受安全过滤保护），返回 stdout/stderr/退出码/是否超时"""
        return await self._run("执行命令失败", lambda ssh, c, t: ssh.BatchCMD(c, t),
                               uuid, command, timeout)

    async def ssh_cmd_not_ref(self, uuid: str, command: str, timeout: Optional[int] = None) -> str:
        """执行非交互命令，只关心是否执行完（返回退出码与 stderr）"""
        return await self._run("非交互命令执行失败", lambda ssh, c, t: ssh.BatchCMD_NotRef(c, t),
                               uuid, command, timeout)

    async def ssh_interactive_cmd(self, uuid: str, command: str,
                                  interactions_json: str = "[]",
                                  timeout: Optional[int] = None) -> str:
        """脚本化交互命令：按 interactions_json 顺序「等待提示→发送应答」自动完成。
        interactions_json 示例：
          [{"expect": "password", "send": "我的密码", "timeout": 10},
           {"expect": "continue", "send": "y"}]
        返回完整输出、exit_status、每步 matched 结果。"""
        try:
            interactions = self._parse_interactions(interactions_json)
            async with self._connect(uuid) as ssh:
                res = json.loads(await asyncio.to_thread(
                    ssh.InteractiveCMD, command, interactions, timeout))
            return ResponseFormatter.success(res)
        except ToolError as e:
            return ResponseFormatter.error(str(e))
        except Exception as e:
            logger.exception("交互命令执行异常")
            return ResponseFormatter.error("交互命令执行失败", e)

    async def ssh_system_version(self, uuid: str) -> str:
        """获取系统版本信息（uname -a）"""
        return await self._run("获取系统版本失败", lambda ssh: ssh.GetSystemVersion(), uuid)

    async def ssh_is_connected(self, uuid: str) -> str:
        """检测目标主机 SSH 连接是否有效（建连即有效，返回连接状态）"""
        try:
            async with self._connect(uuid) as ssh:
                ok = await asyncio.to_thread(ssh.IsConnected)
            return ResponseFormatter.success({"uuid": uuid, "connected": ok})
        except ToolError as e:
            return ResponseFormatter.error(str(e))
        except Exception as e:
            logger.exception("连接检测异常")
            return ResponseFormatter.error("连接检测失败", e)

    # ---- 安全过滤（进程级，作用于后续所有 ssh_* 调用） ----

    async def ssh_filter_set(self, mode: str) -> str:
        """设置命令过滤模式：off / blacklist / whitelist（进程级，作用于后续所有调用）"""
        if mode not in ("off", "blacklist", "whitelist"):
            return ResponseFormatter.error(f"非法过滤模式：{mode}（可选 off/blacklist/whitelist）")
        self.filter_mode = mode
        return ResponseFormatter.success({"mode": mode,
                                          "extra_blacklist": self._extra_blacklist,
                                          "extra_whitelist": self._extra_whitelist})

    async def ssh_filter_add_blacklist(self, rules: List[str]) -> str:
        """追加黑名单规则（进程级；纯命令名按段首命令匹配，含正则元字符的原样作为正则）"""
        added = [r for r in rules if r and r not in self._extra_blacklist]
        self._extra_blacklist.extend(added)
        return ResponseFormatter.success({"added": added, "count": len(self._extra_blacklist)})

    async def ssh_filter_add_whitelist(self, rules: List[str]) -> str:
        """追加白名单规则（进程级）"""
        added = [r for r in rules if r and r not in self._extra_whitelist]
        self._extra_whitelist.extend(added)
        return ResponseFormatter.success({"added": added, "count": len(self._extra_whitelist)})

    async def ssh_filter_get(self) -> str:
        """查看当前命令过滤策略（模式 + 追加规则）"""
        return ResponseFormatter.success({
            "mode": self.filter_mode,
            "extra_blacklist": self._extra_blacklist,
            "extra_whitelist": self._extra_whitelist,
            "default_blacklist_count": 20,
            "default_whitelist_count": 83,
        })

    # ---- SFTP 文件操作 ----

    async def ssh_read_file(self, uuid: str, remote_file_path: str, encoding: str = "utf-8") -> str:
        """读取远程文本文件全文"""
        return await self._run("读取远程文件失败",
                               lambda ssh, p, e: ssh.read_remote_file(p, e),
                               uuid, remote_file_path, encoding)

    async def ssh_write_file(self, uuid: str, remote_file_path: str, content: str,
                             mode: str = "w", encoding: str = "utf-8") -> str:
        """写入/追加远程文件（mode: w 覆盖 / a 追加 / wb / ab）"""
        return await self._run("写入远程文件失败",
                               lambda ssh, p, c, m, e: ssh.write_remote_file(p, c, m, e),
                               uuid, remote_file_path, content, mode, encoding)

    async def ssh_get_file(self, uuid: str, remote_path: str, local_path: str) -> str:
        """下载远程文件到本地（local_path 为本机路径）"""
        return await self._run("下载远程文件失败",
                               lambda ssh, r, l: ssh.GetRemoteFile(r, l),
                               uuid, remote_path, local_path)

    async def ssh_put_file(self, uuid: str, localpath: str, remotepath: str) -> str:
        """上传本地文件到远程（localpath 为本机路径）"""
        return await self._run("上传本地文件失败",
                               lambda ssh, l, r: ssh.PutLocalFile(l, r),
                               uuid, localpath, remotepath)

    async def ssh_list_dir(self, uuid: str, remote_path: str = ".") -> str:
        """列出远程目录内容（名称/大小/时间/是否目录）"""
        return await self._run("列目录失败",
                               lambda ssh, p: ssh.ListRemoteDir(p),
                               uuid, remote_path)

    async def ssh_make_dir(self, uuid: str, remote_path: str) -> str:
        """在远程创建目录"""
        return await self._run("创建远程目录失败",
                               lambda ssh, p: ssh.MakeRemoteDir(p),
                               uuid, remote_path)

    async def ssh_remove_file(self, uuid: str, remote_path: str) -> str:
        """删除远程文件（明确授权的文件操作，不受命令黑名单影响）"""
        return await self._run("删除远程文件失败",
                               lambda ssh, p: ssh.RemoveRemoteFile(p),
                               uuid, remote_path)

    async def ssh_remove_dir(self, uuid: str, remote_path: str) -> str:
        """删除远程空目录"""
        return await self._run("删除远程目录失败",
                               lambda ssh, p: ssh.RemoveRemoteDir(p),
                               uuid, remote_path)

    async def ssh_stat_file(self, uuid: str, remote_path: str) -> str:
        """查看远程文件/目录属性（大小/时间/类型）"""
        return await self._run("查看远程属性失败",
                               lambda ssh, p: ssh.StatRemoteFile(p),
                               uuid, remote_path)


# ---------- 多会话工具（SSHSessionManager） ----------

class SessionTools:
    """SSHSessionManager 多会话能力 → MCP 工具（session_*）
    管理器为进程级单例：会话连接由管理器持有，可跨工具调用持续存在。
    所有操作串行化执行（asyncio.Lock），避免并发读写冲突。
    """

    def __init__(self, config: ServerConfig):
        self.config = config
        self.db = AdminDataBase(config.database_path)
        self.manager = SSHSessionManager()
        self._lock = asyncio.Lock()

    async def _call(self, fn, *args) -> Dict[str, Any]:
        """串行化 + 线程池执行管理器同步方法，返回解析后的 dict"""
        async with self._lock:
            res = await asyncio.to_thread(fn, *args)
        return json.loads(res) if isinstance(res, str) else res

    async def _wrap(self, action: str, fn, *args) -> str:
        try:
            result = await self._call(fn, *args)
            return ResponseFormatter.success(result)
        except Exception as e:
            logger.exception("%s异常", action)
            return ResponseFormatter.error(action, e)

    # ---- 生命周期 ----

    async def session_create(self, host: str, username: str, password: str,
                             port: int = 22, name: Optional[str] = None) -> str:
        """新建交互式 shell 会话窗体（连接由管理器持有；关闭时释放）"""
        return await self._wrap("创建会话失败",
                                self.manager.CreateSession, host, username, password,
                                port, name)

    async def session_create_from_db(self, uuid: str, name: Optional[str] = None) -> str:
        """按数据库中的主机 UUID 创建会话窗体"""
        try:
            host = await asyncio.to_thread(self._find_host, uuid)
            res = await self._call(self.manager.CreateSession,
                                   host["address"], host["username"], host["password"],
                                   int(host["port"]), name)
            return ResponseFormatter.success(res)
        except ToolError as e:
            return ResponseFormatter.error(str(e))
        except Exception as e:
            logger.exception("按数据库创建会话异常")
            return ResponseFormatter.error("按数据库创建会话失败", e)

    def _find_host(self, uuid: str) -> Dict[str, str]:
        res = self.db.ShowHostList()
        if res["code"] != 1:
            raise ToolError(f"查询主机列表失败：{res['msg']}")
        for host in res["data"]:
            if host["uuid"] == uuid:
                return host
        raise ToolError(f"UUID {uuid} 对应的主机不存在")

    async def session_close(self, session_id: str) -> str:
        """关闭指定会话窗体（自建连接随之释放）"""
        return await self._wrap("关闭会话失败", self.manager.CloseSession, session_id)

    async def session_close_all(self) -> str:
        """关闭并清空所有会话窗体"""
        return await self._wrap("关闭全部会话失败", self.manager.CloseAll)

    async def session_start_drainer(self) -> str:
        """启动后台输出收取线程（创建会话时自动启动，一般无需手动）"""
        return await self._wrap("启动后台收取失败", self.manager.StartDrainer)

    async def session_stop_drainer(self) -> str:
        """停止后台输出收取线程"""
        return await self._wrap("停止后台收取失败", self.manager.StopDrainer)

    # ---- 遍历 / 切换 ----

    async def session_list(self) -> str:
        """遍历当前所有会话（数量、激活窗体、每个窗体的信息与最新输出）"""
        return await self._wrap("遍历会话失败", self.manager.ListSessions)

    async def session_count(self) -> str:
        """统计当前会话数量"""
        return await self._wrap("统计会话数量失败", self.manager.GetSessionCount)

    async def session_info(self, session_id: Optional[str] = None) -> str:
        """查看指定会话信息（缺省为当前激活会话）"""
        return await self._wrap("查看会话信息失败", self.manager.GetSessionInfo, session_id)

    async def session_switch(self, session_id: str) -> str:
        """切入指定会话（原会话切出后后台保留、命令继续执行）"""
        return await self._wrap("切换会话失败", self.manager.SwitchSession, session_id)

    async def session_active(self) -> str:
        """获取当前激活（切入）的会话信息"""
        return await self._wrap("获取激活会话失败", self.manager.GetActiveSession)

    # ---- 读写 / 执行 ----

    async def session_read(self, session_id: Optional[str] = None, timeout: int = 1) -> str:
        """读取指定会话（缺省激活会话）自本次调用起新增的输出"""
        return await self._wrap("读取会话输出失败", self.manager.Read, session_id, timeout)

    async def session_send(self, session_id: Optional[str] = None, text: str = "") -> str:
        """向指定会话发送输入（自动补回车）"""
        return await self._wrap("发送输入失败", self.manager.Send, session_id, text)

    async def session_execute(self, session_id: Optional[str] = None,
                              command: str = "", wait: float = 2.0) -> str:
        """在指定会话中执行命令并读取一段时间输出（受安全过滤保护，危险命令返回 403）"""
        return await self._wrap("执行命令失败", self.manager.Execute, session_id, command, wait)

    async def session_wait_prompt(self, session_id: Optional[str] = None,
                                  pattern: str = r"[#$]\s*$", timeout: int = 30) -> str:
        """等待指定会话输出中出现提示符（正则），用于确认命令执行完毕"""
        return await self._wrap("等待提示符失败", self.manager.WaitPrompt,
                                session_id, pattern, timeout)

    # ---- 命令状态检测 ----

    async def session_state(self, session_id: Optional[str] = None,
                            timeout: int = 2, use_probe: bool = True) -> str:
        """检测会话命令执行状态：idle（空闲/执行完毕）/ busy（正在执行）/ closed（已关闭）"""
        return await self._wrap("检测命令状态失败", self.manager.GetCommandState,
                                session_id, timeout, use_probe)

    async def session_wait_finish(self, session_id: Optional[str] = None,
                                  timeout: int = 30, interval: int = 1) -> str:
        """阻塞等待会话当前命令执行完毕（提示符重现即返回）"""
        return await self._wrap("等待命令执行完毕失败", self.manager.WaitCommandFinish,
                                session_id, timeout, interval)

    # ---- 会话间消息 ----

    async def session_post_message(self, to_session_id: str, message: str,
                                   from_id: str = "manager") -> str:
        """向指定会话投递消息（进入其收件箱，不干扰终端输入）"""
        return await self._wrap("投递消息失败", self.manager.PostMessage,
                                to_session_id, message, from_id)

    async def session_read_messages(self, session_id: Optional[str] = None,
                                    clear: bool = True) -> str:
        """读取指定会话收件箱消息（默认读后清空）"""
        return await self._wrap("读取消息失败", self.manager.ReadMessages, session_id, clear)

    async def session_send_to(self, to_session_id: str, text: str,
                              from_session_id: Optional[str] = None) -> str:
        """把一个窗体的输入直接注入到另一窗体（如同在目标窗体键入并回车执行）"""
        return await self._wrap("注入输入失败", self.manager.SendToSession,
                                to_session_id, text, from_session_id)

    async def session_broadcast(self, message: str, from_id: str = "manager") -> str:
        """向所有会话广播消息（收件箱方式）"""
        return await self._wrap("广播消息失败", self.manager.Broadcast, message, from_id)

    # ---- 历史 / 持久化 ----

    async def session_history(self, session_id: Optional[str] = None, tail: int = 20) -> str:
        """获取指定会话命令历史（默认最近 20 条）"""
        return await self._wrap("获取命令历史失败", self.manager.GetHistory, session_id, tail)

    async def session_export(self, path: Optional[str] = None,
                             include_password: bool = True) -> str:
        """导出全部会话配置与命令历史到 JSON（include_password=True 时含明文密码，请妥善保管）"""
        return await self._wrap("导出会话失败", self.manager.ExportSessions, path, include_password)

    async def session_restore(self, path: str = "sessions_backup.json",
                              connect: bool = True) -> str:
        """从备份文件恢复会话（connect=True 重新连接；False 为 dry-run 校验）"""
        return await self._wrap("恢复会话失败", self.manager.RestoreSessions, path, connect)


# ---------- 工具注册 ----------

def register_tools(mcp: MCPServer, config: ServerConfig) -> None:
    """把三类工具全部注册到 MCP 服务器"""
    db_tools = DatabaseTools(config)
    ssh_tools = SSHTools(config)
    session_tools = SessionTools(config)

    db_methods = [
        "db_init", "host_list", "host_add", "host_modify", "host_modify_ip",
        "host_modify_user_pwd", "host_modify_port", "host_delete", "host_batch_delete",
        "host_search_uuid", "host_search_address", "host_fuzzy_search", "host_check",
        "host_count", "group_count", "group_check", "group_add", "group_delete",
        "group_rename", "group_add_host", "group_remove_host", "group_batch_add",
        "group_batch_remove", "group_clear", "group_show_all", "group_show",
        "group_show_single", "ping_group", "ping_all", "db_backup", "db_restore",
        "db_export_csv", "db_export_json", "db_import_csv", "db_clear_hosts",
        "db_clear_groups",
    ]
    ssh_methods = [
        "ssh_cmd", "ssh_cmd_not_ref", "ssh_interactive_cmd", "ssh_system_version",
        "ssh_is_connected", "ssh_filter_set", "ssh_filter_add_blacklist",
        "ssh_filter_add_whitelist", "ssh_filter_get", "ssh_read_file",
        "ssh_write_file", "ssh_get_file", "ssh_put_file", "ssh_list_dir",
        "ssh_make_dir", "ssh_remove_file", "ssh_remove_dir", "ssh_stat_file",
    ]
    session_methods = [
        "session_create", "session_create_from_db", "session_list", "session_count",
        "session_info", "session_switch", "session_active", "session_read",
        "session_send", "session_execute", "session_wait_prompt", "session_state",
        "session_wait_finish", "session_post_message", "session_read_messages",
        "session_send_to", "session_broadcast", "session_history", "session_export",
        "session_restore", "session_start_drainer", "session_stop_drainer",
        "session_close", "session_close_all",
    ]

    for name in db_methods:
        mcp.tool(name=name)(getattr(db_tools, name))
    for name in ssh_methods:
        mcp.tool(name=name)(getattr(ssh_tools, name))
    for name in session_methods:
        mcp.tool(name=name)(getattr(session_tools, name))


# ---------- 入口 ----------

def parse_arguments():
    parser = argparse.ArgumentParser(description="AI SSH MCP Server")
    parser.add_argument("--config", type=str, help="配置文件路径", default="config.json")
    parser.add_argument("--host", type=str, help="服务监听地址")
    parser.add_argument("--port", type=int, help="服务监听端口")
    parser.add_argument("--log-level", type=str, help="日志级别",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--list-tools", action="store_true",
                        help="打印已注册工具清单后退出（不启动服务）")
    return parser.parse_args()


def main():
    args = parse_arguments()

    config = ServerConfig()
    config.load_from_file(args.config)
    if args.host:
        config.mcp_host = args.host
    if args.port:
        config.mcp_port = args.port
    if args.log_level:
        config.log_level = args.log_level
        logging.getLogger().setLevel(args.log_level)

    mcp = MCPServer(
        name=config.mcp_service_id,
        title="AI SSH 运维工具",
        instructions=config.system_prompt,
        version="2.0.0",
        log_level=config.log_level,
    )

    try:
        register_tools(mcp, config)
    except Exception as e:
        logger.error("工具注册失败: %s", e, exc_info=True)
        sys.exit(1)

    async def _collect_tools():
        return await mcp.list_tools()

    tools = asyncio.run(_collect_tools())
    logger.info("MCP 服务就绪：已注册 %d 个工具（数据库 %d / 单机SSH %d / 多会话 %d）",
                len(tools), 36, 18, 24)
    if args.list_tools:
        for t in tools:
            print(f"{t.name:24s} {t.description or ''}")
        return

    try:
        logger.info("启动 MCP 服务：%s://%s:%s （transport=%s）",
                    config.mcp_transport, config.mcp_host, config.mcp_port,
                    config.mcp_transport)
        mcp.run(transport=config.mcp_transport,
                host=config.mcp_host,
                port=config.mcp_port)
    except Exception as e:
        logger.error("服务运行出错: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
