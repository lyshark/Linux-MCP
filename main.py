# -*- coding: utf-8 -*-
import argparse
import asyncio
import json
import logging
import os
import platform
import signal
import sys
from typing import TypeAlias, Union, Optional, Dict, Any, List

from AdminDataBase import AdminDataBase
from MySSHBase import MySSH
from mcp.server.fastmcp import FastMCP

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('server.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

class ServerConfig:
    def __init__(self):
        self.mcp_service_id = "aissh_mcp_server_cherry"
        self.mcp_host = "127.0.0.1"
        self.mcp_port = 8001
        self.mcp_transport = "streamable-http"
        self.log_level = "INFO"
        self.auto_approve_tools = True
        self.timeout = 1800
        self.database_path = "admin.db"
        self.system_prompt = """
        这是一个LinuxSSH自动化执行工具，通过SSH连接远程Linux服务器执行命令并返回结果。
        严格遵循的规范：
            0.请全程使用简体中文回复。
            1.请自主决策完成用户下达的任务。
            2.禁止执行任何的交互式命令，只允许执行非交互命令。
            3.请在每次调用命令之前输出一段简单的提示信息，标注这一步做什么。
            4.请在收到用户命令后迭代执行，全程由你自主做决策，直到解决问题。
            5.当命令输出数据量过大时（超过配置阈值），仅返回执行状态（成功/失败），不返回具体内容。
        原则：
            仅能够执行Linux系统下的读取命令，禁止执行任何修改命令。
        """

    def load_from_file(self, config_path: str) -> None:
        """从JSON配置文件加载配置"""
        if not os.path.exists(config_path):
            logger.warning(f"配置文件 {config_path} 不存在，将使用默认配置")
            return

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)

            # 更新配置属性
            for key, value in config_data.items():
                if hasattr(self, key):
                    setattr(self, key, value)
                    logger.info(f"从配置文件加载 {key}: {value}")
        except Exception as e:
            logger.error(f"加载配置文件失败: {str(e)}，将使用默认配置")


class ResponseFormatter:
    @staticmethod
    def success(result: Any) -> str:
        return json.dumps({
            "status": "success",
            "result": result
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def error(message: str, details: Optional[Any] = None) -> str:
        response = {
            "status": "error",
            "message": f"{message}（详情：{str(details)}）" if details else message
        }
        return json.dumps(response, ensure_ascii=False, indent=2)


Number: TypeAlias = Union[int, float]


class DataBaseInfo:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.db = AdminDataBase(config.database_path)

        # 初始化数据库
        init_result = self.db.InitDatabase()
        if init_result["code"] == 1:
            logger.info(init_result["msg"])
        else:
            logger.warning(init_result["msg"])

    async def show_host_list(self) -> str:
        """显示所有主机列表"""
        try:
            result = self.db.ShowHostList()
            return ResponseFormatter.success(result)
        except Exception as e:
            logger.error(f"显示主机列表失败: {str(e)}")
            return ResponseFormatter.error("显示主机列表失败", e)

    async def add_host(self, uuid: str, address: str, username: str, password: str, port: str) -> str:
        """添加新主机"""
        try:
            result = self.db.AddHost(uuid, address, username, password, port)
            return ResponseFormatter.success(result)
        except Exception as e:
            logger.error(f"添加主机失败: {str(e)}")
            return ResponseFormatter.error("添加主机失败", e)

    async def delete_host(self, uuid: str) -> str:
        """删除指定主机"""
        try:
            result = self.db.DeleteHost(uuid)
            return ResponseFormatter.success(result)
        except Exception as e:
            logger.error(f"删除主机失败: {str(e)}")
            return ResponseFormatter.error("删除主机失败", e)

    async def backup_database(self, backup_path: Optional[str] = None) -> str:
        """备份数据库"""
        try:
            result = self.db.BackupDatabase(backup_path)
            return ResponseFormatter.success(result)
        except Exception as e:
            logger.error(f"备份数据库失败: {str(e)}")
            return ResponseFormatter.error("备份数据库失败", e)

    async def modify_host(self, uuid: str, address: str, username: str, password: str, port: str) -> str:
        """全量修改主机信息"""
        try:
            result = self.db.ModifyHost(uuid, address, username, password, port)
            return ResponseFormatter.success(result)
        except Exception as e:
            logger.error(f"修改主机失败: {str(e)}")
            return ResponseFormatter.error("修改主机失败", e)

    async def modify_host_ip(self, uuid: str, new_address: str) -> str:
        """单独修改主机IP地址"""
        try:
            result = self.db.ModifyHostIp(uuid, new_address)
            return ResponseFormatter.success(result)
        except Exception as e:
            logger.error(f"修改主机IP失败: {str(e)}")
            return ResponseFormatter.error("修改主机IP失败", e)

    async def modify_host_user_pwd(self, uuid: str, new_username: str, new_password: str) -> str:
        """单独修改主机账号和密码"""
        try:
            result = self.db.ModifyHostUserPwd(uuid, new_username, new_password)
            return ResponseFormatter.success(result)
        except Exception as e:
            logger.error(f"修改主机账号密码失败: {str(e)}")
            return ResponseFormatter.error("修改主机账号密码失败", e)

    async def modify_host_port(self, uuid: str, new_port: str) -> str:
        """单独修改主机端口号"""
        try:
            result = self.db.ModifyHostPort(uuid, new_port)
            return ResponseFormatter.success(result)
        except Exception as e:
            logger.error(f"修改主机端口失败: {str(e)}")
            return ResponseFormatter.error("修改主机端口失败", e)

class SSHBaseInfo:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.db = AdminDataBase(config.database_path)  # 用于查询主机信息

    def _get_host_info(self, uuid: str) -> Optional[Dict[str, str]]:
        """根据UUID查询主机信息"""
        try:
            host_list = self.db.ShowHostList()
            if host_list["code"] != 1:
                logger.error(f"查询主机列表失败: {host_list['msg']}")
                return None
            # 从主机列表中筛选目标UUID的主机
            for host in host_list["data"]:
                if host["uuid"] == uuid:
                    return host
            logger.warning(f"未找到UUID为 {uuid} 的主机")
            return None
        except Exception as e:
            logger.error(f"获取主机信息失败: {str(e)}")
            return None

    async def batch_cmd(self, uuid: str, command: str) -> str:
        """执行Linux系统命令并返回结果"""
        try:
            host_info = self._get_host_info(uuid)
            if not host_info:
                return ResponseFormatter.error(f"UUID {uuid} 对应的主机不存在")

            # 初始化SSH连接
            ssh = MySSH(
                address=host_info["address"],
                username=host_info["username"],
                password=host_info["password"],
                default_port=int(host_info["port"])  # 数据库中port为字符串，需转为int
            )
            init_result = json.loads(ssh.Init())
            if init_result["code"] != 200:
                return ResponseFormatter.error(f"SSH连接失败: {init_result['msg']}")

            # 执行命令
            cmd_result = json.loads(ssh.BatchCMD(command))
            ssh.CloseSSH()  # 确保连接关闭

            return ResponseFormatter.success(cmd_result) if cmd_result["code"] == 200 \
                else ResponseFormatter.error(f"命令执行失败: {cmd_result['msg']}")
        except Exception as e:
            logger.error(f"批量命令执行异常: {str(e)}")
            return ResponseFormatter.error("批量命令执行失败", e)

    async def batch_cmd_not_ref(self, uuid: str, command: str) -> str:
        """执行Linux系统命令，并返回状态值"""
        try:
            host_info = self._get_host_info(uuid)
            if not host_info:
                return ResponseFormatter.error(f"UUID {uuid} 对应的主机不存在")

            ssh = MySSH(
                address=host_info["address"],
                username=host_info["username"],
                password=host_info["password"],
                default_port=int(host_info["port"])
            )
            init_result = json.loads(ssh.Init())
            if init_result["code"] != 200:
                return ResponseFormatter.error(f"SSH连接失败: {init_result['msg']}")

            cmd_result = json.loads(ssh.BatchCMD_NotRef(command))
            ssh.CloseSSH()

            return ResponseFormatter.success(cmd_result) if cmd_result["code"] == 200 \
                else ResponseFormatter.error(f"非交互命令执行失败: {cmd_result['msg']}")
        except Exception as e:
            logger.error(f"非交互命令执行异常: {str(e)}")
            return ResponseFormatter.error("非交互命令执行失败", e)

    async def get_remote_file(self, uuid: str, remote_path: str, local_path: str) -> str:
        """下载远程文件到本地"""
        try:
            host_info = self._get_host_info(uuid)
            if not host_info:
                return ResponseFormatter.error(f"UUID {uuid} 对应的主机不存在")

            ssh = MySSH(
                address=host_info["address"],
                username=host_info["username"],
                password=host_info["password"],
                default_port=int(host_info["port"])
            )
            init_result = json.loads(ssh.Init())
            if init_result["code"] != 200:
                return ResponseFormatter.error(f"SSH连接失败: {init_result['msg']}")

            file_result = json.loads(ssh.GetRemoteFile(remote_path, local_path))
            ssh.CloseSSH()

            return ResponseFormatter.success(file_result) if file_result["code"] == 200 \
                else ResponseFormatter.error(f"文件下载失败: {file_result['msg']}")
        except Exception as e:
            logger.error(f"远程文件下载异常: {str(e)}")
            return ResponseFormatter.error("远程文件下载失败", e)

    async def put_local_file(self, uuid: str, localpath: str, remotepath: str) -> str:
        """上传本地文件到远程"""
        try:
            host_info = self._get_host_info(uuid)
            if not host_info:
                return ResponseFormatter.error(f"UUID {uuid} 对应的主机不存在")

            ssh = MySSH(
                address=host_info["address"],
                username=host_info["username"],
                password=host_info["password"],
                default_port=int(host_info["port"])
            )
            init_result = json.loads(ssh.Init())
            if init_result["code"] != 200:
                return ResponseFormatter.error(f"SSH连接失败: {init_result['msg']}")

            file_result = json.loads(ssh.PutLocalFile(localpath, remotepath))
            ssh.CloseSSH()

            return ResponseFormatter.success(file_result) if file_result["code"] == 200 \
                else ResponseFormatter.error(f"文件上传失败: {file_result['msg']}")
        except Exception as e:
            logger.error(f"本地文件上传异常: {str(e)}")
            return ResponseFormatter.error("本地文件上传失败", e)

    async def get_system_version(self, uuid: str) -> str:
        """获取系统版本基本信息"""
        try:
            host_info = self._get_host_info(uuid)
            if not host_info:
                return ResponseFormatter.error(f"UUID {uuid} 对应的主机不存在")

            ssh = MySSH(
                address=host_info["address"],
                username=host_info["username"],
                password=host_info["password"],
                default_port=int(host_info["port"])
            )
            init_result = json.loads(ssh.Init())
            if init_result["code"] != 200:
                return ResponseFormatter.error(f"SSH连接失败: {init_result['msg']}")

            version_result = json.loads(ssh.GetSystemVersion())
            ssh.CloseSSH()

            return ResponseFormatter.success(version_result) if version_result["code"] == 200 \
                else ResponseFormatter.error(f"获取系统版本失败: {version_result['msg']}")
        except Exception as e:
            logger.error(f"系统版本获取异常: {str(e)}")
            return ResponseFormatter.error("获取系统版本失败", e)

    async def close_ssh(self, uuid: str) -> str:
        """关闭SSH连接（适配MySSH.CloseSSH）"""
        try:
            host_info = self._get_host_info(uuid)
            if not host_info:
                return ResponseFormatter.error(f"UUID {uuid} 对应的主机不存在")

            ssh = MySSH(
                address=host_info["address"],
                username=host_info["username"],
                password=host_info["password"],
                default_port=int(host_info["port"])
            )
            close_result = json.loads(ssh.CloseSSH())
            return ResponseFormatter.success(close_result)
        except Exception as e:
            logger.error(f"SSH连接关闭异常: {str(e)}")
            return ResponseFormatter.error("关闭SSH连接失败", e)

    async def read_remote_file(self, uuid: str, remote_file_path: str, encoding: str = "utf-8") -> str:
        """读取远程文件内容,用于文本文件"""
        try:
            host_info = self._get_host_info(uuid)
            if not host_info:
                return ResponseFormatter.error(f"UUID {uuid} 对应的主机不存在")

            ssh = MySSH(
                address=host_info["address"],
                username=host_info["username"],
                password=host_info["password"],
                default_port=int(host_info["port"])
            )
            init_result = json.loads(ssh.Init())
            if init_result["code"] != 200:
                return ResponseFormatter.error(f"SSH连接失败: {init_result['msg']}")

            read_result = json.loads(ssh.read_remote_file(remote_file_path, encoding))
            ssh.CloseSSH()

            return ResponseFormatter.success(read_result) if read_result["code"] == 200 \
                else ResponseFormatter.error(f"读取文件失败: {read_result['msg']}")
        except Exception as e:
            logger.error(f"远程文件读取异常: {str(e)}")
            return ResponseFormatter.error("读取远程文件失败", e)

    async def write_remote_file(self, uuid: str, remote_file_path: str, content: str, mode: str = "w", encoding: str = "utf-8") -> str:
        """写入远程文件,用于文本文件"""
        try:
            host_info = self._get_host_info(uuid)
            if not host_info:
                return ResponseFormatter.error(f"UUID {uuid} 对应的主机不存在")

            ssh = MySSH(
                address=host_info["address"],
                username=host_info["username"],
                password=host_info["password"],
                default_port=int(host_info["port"])
            )
            init_result = json.loads(ssh.Init())
            if init_result["code"] != 200:
                return ResponseFormatter.error(f"SSH连接失败: {init_result['msg']}")

            write_result = json.loads(ssh.write_remote_file(remote_file_path, content, mode, encoding))
            ssh.CloseSSH()

            return ResponseFormatter.success(write_result) if write_result["code"] == 200 \
                else ResponseFormatter.error(f"写入文件失败: {write_result['msg']}")
        except Exception as e:
            logger.error(f"远程文件写入异常: {str(e)}")
            return ResponseFormatter.error("写入远程文件失败", e)


def register_tools(mcp: FastMCP, config: ServerConfig):
    # 数据库工具注册
    info_tools = DataBaseInfo(config)
    mcp.tool()(info_tools.show_host_list)
    mcp.tool()(info_tools.add_host)
    mcp.tool()(info_tools.modify_host)
    mcp.tool()(info_tools.modify_host_ip)
    mcp.tool()(info_tools.modify_host_user_pwd)
    mcp.tool()(info_tools.modify_host_port)
    mcp.tool()(info_tools.delete_host)

    # SSH工具注册（新增）
    ssh_tools = SSHBaseInfo(config)
    mcp.tool()(ssh_tools.batch_cmd)
    mcp.tool()(ssh_tools.batch_cmd_not_ref)
    mcp.tool()(ssh_tools.get_remote_file)
    mcp.tool()(ssh_tools.put_local_file)
    mcp.tool()(ssh_tools.get_system_version)
    mcp.tool()(ssh_tools.close_ssh)
    mcp.tool()(ssh_tools.read_remote_file)
    mcp.tool()(ssh_tools.write_remote_file)

def parse_arguments():
    parser = argparse.ArgumentParser(description='AISS H MCP Server')
    parser.add_argument('--config', type=str, help='配置文件路径', default='config.json')
    parser.add_argument('--host', type=str, help='服务监听地址')
    parser.add_argument('--port', type=int, help='服务监听端口')
    parser.add_argument('--log-level', type=str, help='日志级别',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'])
    return parser.parse_args()

def handle_shutdown(signal, frame, mcp, loop):
    logger.info("收到关闭信号，正在优雅退出...")
    if loop.is_running():
        loop.create_task(mcp.stop())
        loop.stop()
    sys.exit(0)

def main():
    # 解析命令行参数
    args = parse_arguments()

    # 初始化配置
    config = ServerConfig()
    config.load_from_file(args.config)

    # 用命令行参数覆盖配置
    if args.host:
        config.mcp_host = args.host
    if args.port:
        config.mcp_port = args.port
    if args.log_level:
        config.log_level = args.log_level
        logger.setLevel(args.log_level)

    # 初始化MCP服务
    try:
        mcp = FastMCP(
            name=config.mcp_service_id,
            host=config.mcp_host,
            port=config.mcp_port,
            log_level=config.log_level
        )
        logger.info(f"成功初始化MCP服务（ID：{config.mcp_service_id}，地址：{config.mcp_host}:{config.mcp_port}）")
    except OSError as e:
        logger.error(f"服务初始化失败：端口「{config.mcp_port}」相关错误: {str(e)}", exc_info=True)
        sys.exit(1)
    except Exception as e:
        logger.error(f"服务初始化失败：未知错误: {str(e)}", exc_info=True)
        sys.exit(1)

    # 注册工具
    try:
        register_tools(mcp, config)
    except Exception as e:
        logger.error(f"工具注册失败: {str(e)}", exc_info=True)
        sys.exit(1)

    # 设置事件循环
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    # 仅在非Windows系统注册信号处理（Windows不支持）
    if platform.system() != "Windows":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handle_shutdown, sig, None, mcp, loop)
            except Exception as e:
                logger.warning(f"无法注册信号处理器: {e}")

    # 运行服务
    try:
        if loop.is_running():
            loop.create_task(mcp.run(transport=config.mcp_transport))
            loop.run_forever()
        else:
            loop.run_until_complete(mcp.run(transport=config.mcp_transport))
    except Exception as e:
        logger.error(f"服务运行出错: {str(e)}", exc_info=True)
        sys.exit(1)
    finally:
        loop.close()
        logger.info("服务已停止")

if __name__ == "__main__":
    main()