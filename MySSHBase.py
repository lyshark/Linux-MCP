import json
import re
import time
from stat import S_ISDIR

import paramiko


# 默认黑名单规则：
#   纯命令名（如 "rm"）自动按「段首命令」匹配，可带 sudo 前缀，管道/串联命令逐段检查；
#   含正则元字符的条目（如 r"init\s+[06]\b"）原样作为正则全文匹配。
DEFAULT_BLACKLIST = [
    # 删除类
    "rm", "rmdir", "del", "rd", "format",
    # 磁盘 / 分区 / 格式化
    "dd", "mkfs", "mke2fs", "fdisk", "sfdisk", "parted",
    # 关机 / 重启
    "shutdown", "reboot", "halt", "poweroff", r"init\s+[06]\b", r"telinit\s+[06]\b",
    # 高危操作
    r"kill\s+-9\s+(-1|0|1)\b",          # 杀掉所有/系统进程
    r":\s*\(\s*\)\s*\{\s*[:|]",           # fork 炸弹
    r"(>|>>)\s*/dev/(sd|vd|nvme|hd)",     # 直接写块设备
]

# 默认白名单（启用 whitelist 模式时的初始允许命令，可按需增删）
DEFAULT_WHITELIST = [
    "ls", "cat", "echo", "pwd", "date", "uptime", "whoami", "id", "hostname",
    "uname", "df", "du", "free", "ps", "top", "netstat", "ss", "ip", "ifconfig",
    "ping", "grep", "head", "tail", "less", "more", "wc", "find", "which", "whereis",
    "curl", "wget", "git", "systemctl", "service", "journalctl", "dmesg", "lsof",
    "tar", "gzip", "gunzip", "zip", "unzip", "cp", "mkdir", "touch", "chmod", "chown",
    "useradd", "usermod", "userdel", "groupadd", "groupdel", "passwd",
    "yum", "dnf", "apt", "apt-get",
    "sed", "awk", "cut", "sort", "uniq", "tr", "xargs", "env", "export", "source",
    "cd", "mount", "umount", "rsync", "scp", "nano", "vim", "vi", "tree",
    "md5sum", "sha256sum", "crontab", "nohup", "sleep", "true", "false",
]


class CommandFilterError(Exception):
    """命令被安全策略拦截时抛出（InvokeCMD 等返回对象的接口使用）"""

    def __init__(self, command, mode, rule):
        self.command = command
        self.mode = mode
        self.rule = rule
        super().__init__(f"命令被安全策略拦截（{mode}）「{rule}」：{command}")


class SSHInteractiveSession:
    """交互式命令会话：逐段读取输出、随时发送输入、按提示等待应答。

    典型用法（配合 MySSH.InvokeCMD）：
        session = ssh.InvokeCMD("sudo apt update")   # 需要 PTY 的交互命令
        session.expect(r"password.*:", timeout=10)    # 等待密码提示
        session.send("我的密码")                       # 发送密码并回车
        print(session.wait_exit())                    # 等命令结束，拿退出码
        session.close()
    """

    def __init__(self, ssh_obj, command, get_pty=True, timeout=30, encoding="utf-8"):
        self.encoding = encoding
        self.timeout = timeout
        self.stdin, self.stdout, self.stderr = ssh_obj.exec_command(
            command, timeout=timeout, get_pty=get_pty)
        self.channel = self.stdout.channel
        self._buf = ""
        self._closed = False

    def _decode(self, data):
        try:
            return data.decode(self.encoding)
        except (UnicodeDecodeError, LookupError):
            return data.decode("utf-8", errors="replace")

    def read(self, timeout=None):
        """读取新到达的输出（内部累计到缓冲区），返回本次新增文本。
        同时收取 stderr：get_pty=False 时 stderr 是独立通道，不读会导致命令阻塞。"""
        if self._closed:
            return ""
        timeout = self.timeout if timeout is None else timeout
        deadline = time.time() + timeout
        chunk = b""
        stderr_ch = self.stderr.channel
        while time.time() < deadline:
            if self.channel.recv_ready():
                data = self.channel.recv(65536)
                if not data:
                    break
                chunk += data
            elif stderr_ch.recv_ready():
                data = stderr_ch.recv(65536)
                if not data:
                    break
                chunk += data
            elif self.channel.exit_status_ready():
                while self.channel.recv_ready():
                    data = self.channel.recv(65536)
                    if not data:
                        break
                    chunk += data
                while stderr_ch.recv_ready():
                    data = stderr_ch.recv(65536)
                    if not data:
                        break
                    chunk += data
                break
            else:
                time.sleep(0.01)
        text = self._decode(chunk)
        self._buf += text
        return text

    def send(self, text, newline=True):
        """向命令发送输入（默认自动补换行，模拟回车）"""
        if not isinstance(text, str):
            text = str(text)
        if newline and not text.endswith("\n"):
            text += "\n"
        self.stdin.write(text)
        self.stdin.flush()

    def send_eof(self):
        """发送 EOF，表示输入结束（如 cat 等待 Ctrl-D 的场景）"""
        try:
            self.stdin.channel.shutdown_write()
        except Exception:
            pass

    def expect(self, pattern, timeout=None):
        """等待输出中出现指定内容（正则），返回匹配到的最新累计输出；超时抛 TimeoutError"""
        timeout = self.timeout if timeout is None else timeout
        regex = re.compile(pattern)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if regex.search(self._buf):
                return self._buf
            if self._closed:
                break
            self.read(0.5)
        raise TimeoutError(f"等待提示「{pattern}」超时，已接收内容：{self._buf[-300:]}")

    def wait_exit(self, timeout=None):
        """等待命令结束并返回退出码；超时返回 None"""
        timeout = self.timeout if timeout is None else timeout
        deadline = time.time() + timeout
        stderr_ch = self.stderr.channel
        while not self.channel.exit_status_ready():
            if self._closed or time.time() > deadline:
                return None
            self.read(0.5)
        while self.channel.recv_ready():
            data = self.channel.recv(65536)
            if not data:
                break
            self._buf += self._decode(data)
        while stderr_ch.recv_ready():
            data = stderr_ch.recv(65536)
            if not data:
                break
            self._buf += self._decode(data)
        try:
            return self.channel.recv_exit_status()
        except Exception:
            return None

    def get_output(self):
        """返回会话累计输出全文"""
        return self._buf.rstrip("\r\n")

    def close(self):
        try:
            self.send_eof()
        except Exception:
            pass
        try:
            self.channel.close()
        except Exception:
            pass
        self._closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class MySSH:
    def __init__(self, address, username, password, default_port=22,
                 connect_timeout=10, command_timeout=30, encoding="utf-8",
                 filter_mode="off", blacklist=None, whitelist=None):
        self.address = address
        self.default_port = default_port
        self.username = username
        self.password = password
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.encoding = encoding
        self.ssh_obj = None
        self.sftp_obj = None
        # 命令安全过滤：off / blacklist / whitelist
        self.filter_mode = filter_mode
        self.blacklist = list(blacklist) if blacklist is not None else list(DEFAULT_BLACKLIST)
        self.whitelist = list(whitelist) if whitelist is not None else list(DEFAULT_WHITELIST)
        self._blacklist_patterns = self._compile_patterns(self.blacklist)
        self._whitelist_patterns = self._compile_patterns(self.whitelist)

    # ==================== 连接管理 ====================

    # 初始化SSH接口
    def Init(self):
        try:
            # 若已有连接，先关闭再重建，避免重复 Init 造成连接泄漏
            if self.ssh_obj is not None:
                try:
                    self.ssh_obj.close()
                except Exception:
                    pass
                self.sftp_obj = None
                self.ssh_obj = None
            self.ssh_obj = paramiko.SSHClient()
            self.ssh_obj.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh_obj.connect(self.address, self.default_port, self.username, self.password,
                                 timeout=self.connect_timeout,
                                 allow_agent=False, look_for_keys=False)
            self.sftp_obj = self.ssh_obj.open_sftp()
            return json.dumps({"code": 200, "msg": "SSH连接初始化成功", "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"SSH连接初始化失败：{e}", "data": False}, ensure_ascii=False)

    # 检测当前连接是否仍然有效
    def IsConnected(self):
        try:
            if self.ssh_obj is not None and self.ssh_obj.get_transport() is not None:
                return bool(self.ssh_obj.get_transport().is_active())
        except Exception:
            pass
        return False

    # 关闭SSH接口（幂等：可重复调用，关闭后对象置空）
    def CloseSSH(self):
        try:
            if self.sftp_obj is not None:
                self.sftp_obj.close()
            if self.ssh_obj is not None:
                self.ssh_obj.close()
            self.sftp_obj = None
            self.ssh_obj = None
            return json.dumps({"code": 200, "msg": "SSH连接已成功关闭", "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"SSH连接关闭失败：{e}", "data": False}, ensure_ascii=False)

    # 支持 with 语法：with MySSH(...) as ssh: 自动连接、退出时自动关闭
    def __enter__(self):
        if not self.IsConnected():
            self.Init()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.CloseSSH()
        return False

    def __del__(self):
        try:
            self.CloseSSH()
        except Exception:
            pass

    # ==================== 内部工具 ====================

    def _ensure_connected(self):
        """未连接时返回错误 JSON，已连接返回 None"""
        if not self.IsConnected():
            return json.dumps({"code": 500, "msg": "SSH未初始化或连接已断开，请先调用 Init()",
                               "data": None, "stderr": None, "exit_status": None,
                               "timed_out": False}, ensure_ascii=False)
        return None

    def _decode(self, data, encoding=None):
        """字节流安全解码，杜绝乱码/抛错"""
        encoding = encoding or self.encoding
        if isinstance(data, str):
            return data
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            return data.decode("utf-8", errors="replace")

    def _exec(self, command, timeout=None, get_pty=False):
        """执行命令，返回 (stdout_bytes, stderr_bytes, exit_status, timed_out)。
        并发读取 stdout/stderr，避免大输出时管道阻塞死锁。"""
        timeout = timeout if timeout is not None else self.command_timeout
        _, stdout, stderr = self.ssh_obj.exec_command(command, timeout=timeout, get_pty=get_pty)
        channel = stdout.channel
        out, err = b"", b""
        deadline = time.time() + timeout
        timed_out = False
        while True:
            while channel.recv_ready():
                out += channel.recv(65536)
            while channel.recv_stderr_ready():
                err += channel.recv_stderr(65536)
            if channel.exit_status_ready():
                while channel.recv_ready():
                    out += channel.recv(65536)
                while channel.recv_stderr_ready():
                    err += channel.recv_stderr(65536)
                break
            if time.time() > deadline:
                timed_out = True
                break
            time.sleep(0.01)
        if timed_out:
            try:
                channel.close()
            except Exception:
                pass
            return out, err, None, True
        try:
            exit_status = channel.recv_exit_status()
        except Exception:
            exit_status = None
        return out, err, exit_status, False

    # ==================== 命令安全过滤 ====================

    @staticmethod
    def _compile_patterns(entries):
        """把规则列表编译为正则。
        纯命令名自动加「段首命令」锚定（可带 sudo 前缀）；含正则元字符的条目原样作为正则。"""
        patterns = []
        for entry in entries or []:
            entry = str(entry).strip()
            if not entry:
                continue
            if any(c in entry for c in "\\^$.*+?[](){}|"):
                patterns.append(re.compile(entry, re.IGNORECASE))
            else:
                patterns.append(re.compile(rf"^\s*(?:sudo\s+)?{re.escape(entry)}\b", re.IGNORECASE))
        return patterns

    def _check_command(self, command):
        """按当前策略检查命令，返回 (是否放行, 命中规则/原因)"""
        if self.filter_mode == "off" or not command or not command.strip():
            return True, None
        # 按 ; && || | 换行 拆分为多段，逐段检查（管道/串联中的危险命令同样拦截）
        segments = re.split(r"[;&|\n]+", command)
        if self.filter_mode == "blacklist":
            for seg in segments:
                seg = seg.strip()
                if not seg:
                    continue
                for pat in self._blacklist_patterns:
                    if pat.search(seg):
                        return False, pat.pattern
            return True, None
        if self.filter_mode == "whitelist":
            for seg in segments:
                seg = seg.strip()
                if not seg:
                    continue
                if not any(p.search(seg) for p in self._whitelist_patterns):
                    token = seg.split()[0] if seg.split() else seg
                    return False, f"命令不在白名单：{token}"
            return True, None
        return True, None

    def _check_and_block(self, command):
        """被拦截时返回 403 JSON，放行返回 None（供各执行方法调用）"""
        allow, rule = self._check_command(command)
        if not allow:
            return json.dumps({
                "code": 403,
                "msg": f"命令被安全策略拦截（{self.filter_mode}）「{rule}」：{command}",
                "data": None, "stderr": None, "exit_status": None, "timed_out": False,
            }, ensure_ascii=False)
        return None

    # 设置命令过滤策略：mode=off/blacklist/whitelist；blacklist/whitelist 传 None 表示保留现有规则
    def SetCommandFilter(self, mode="off", blacklist=None, whitelist=None):
        if mode not in ("off", "blacklist", "whitelist"):
            return json.dumps({"code": 500, "msg": f"非法过滤模式：{mode}（可选 off/blacklist/whitelist）",
                               "data": None}, ensure_ascii=False)
        self.filter_mode = mode
        if blacklist is not None:
            self.blacklist = [str(x).strip() for x in blacklist if str(x).strip()]
            self._blacklist_patterns = self._compile_patterns(self.blacklist)
        if whitelist is not None:
            self.whitelist = [str(x).strip() for x in whitelist if str(x).strip()]
            self._whitelist_patterns = self._compile_patterns(self.whitelist)
        return json.dumps({"code": 200, "msg": f"命令过滤策略已更新：{mode}",
                           "data": {"mode": mode,
                                    "blacklist_count": len(self.blacklist),
                                    "whitelist_count": len(self.whitelist)}},
                          ensure_ascii=False)

    # 追加黑名单规则
    def AddBlacklist(self, *patterns):
        added = 0
        for p in patterns:
            p = str(p).strip()
            if p and p not in self.blacklist:
                self.blacklist.append(p)
                added += 1
        self._blacklist_patterns = self._compile_patterns(self.blacklist)
        return json.dumps({"code": 200, "msg": f"已新增 {added} 条黑名单规则",
                           "data": {"blacklist_count": len(self.blacklist)}}, ensure_ascii=False)

    # 追加白名单规则
    def AddWhitelist(self, *patterns):
        added = 0
        for p in patterns:
            p = str(p).strip()
            if p and p not in self.whitelist:
                self.whitelist.append(p)
                added += 1
        self._whitelist_patterns = self._compile_patterns(self.whitelist)
        return json.dumps({"code": 200, "msg": f"已新增 {added} 条白名单规则",
                           "data": {"whitelist_count": len(self.whitelist)}}, ensure_ascii=False)

    # 查看当前过滤策略
    def GetCommandFilter(self):
        return json.dumps({"code": 200, "msg": "当前命令过滤策略",
                           "data": {"mode": self.filter_mode,
                                    "blacklist": self.blacklist,
                                    "whitelist": self.whitelist}}, ensure_ascii=False)

    # ==================== 命令执行 ====================

    # 执行命令并返回执行结果（含 stdout、stderr、退出码、是否超时）
    def BatchCMD(self, command, timeout=None, get_pty=False):
        try:
            blocked = self._check_and_block(command)
            if blocked:
                return blocked
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            out, err, exit_status, timed_out = self._exec(command, timeout, get_pty)
            stdout_text = self._decode(out).rstrip("\r\n")
            stderr_text = self._decode(err).rstrip("\r\n")
            if timed_out:
                msg = f"命令执行超时（>{timeout if timeout is not None else self.command_timeout}s），可能仍在运行"
            elif exit_status == 0:
                msg = "命令执行成功，已获取结果"
            else:
                msg = f"命令执行失败（退出码 {exit_status}）"
            return json.dumps({
                "code": 200 if exit_status == 0 and not timed_out else 500,
                "msg": msg,
                "data": stdout_text,
                "stderr": stderr_text,
                "exit_status": exit_status,
                "timed_out": timed_out,
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"命令执行失败：{e}",
                               "data": None, "stderr": None, "exit_status": None,
                               "timed_out": False}, ensure_ascii=False)

    # 执行非交互命令，只关心是否执行完（返回退出码与 stderr 供上层判断）
    def BatchCMD_NotRef(self, command, timeout=None):
        try:
            blocked = self._check_and_block(command)
            if blocked:
                return blocked
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            out, err, exit_status, timed_out = self._exec(command, timeout)
            return json.dumps({
                "code": 200,
                "msg": "非交互命令执行成功" if not timed_out else "非交互命令执行超时",
                "data": True,
                "exit_status": exit_status,
                "stderr": self._decode(err).rstrip("\r\n"),
                "timed_out": timed_out,
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"非交互命令执行失败：{e}",
                               "data": False, "exit_status": None}, ensure_ascii=False)

    # 获取系统型号
    def GetSystemVersion(self):
        return self.BatchCMD("uname -a")

    # 启动交互式会话（返回 SSHInteractiveSession 对象，用完后需 close()）
    # 适用于 sudo 密码、菜单选择、逐条询问等无法一次性脚本化的场景
    def InvokeCMD(self, command, get_pty=True, timeout=None):
        allow, rule = self._check_command(command)
        if not allow:
            raise CommandFilterError(command, self.filter_mode, rule)
        err_json = self._ensure_connected()
        if err_json:
            raise RuntimeError(err_json)
        timeout = timeout if timeout is not None else self.command_timeout
        return SSHInteractiveSession(self.ssh_obj, command, get_pty=get_pty,
                                     timeout=timeout, encoding=self.encoding)

    # 脚本化交互命令：按 interactions 顺序「等待提示 → 发送应答」自动执行
    # interactions 每项：{"expect": 提示文字/正则, "send": 应答内容, "timeout": 单步等待秒数}
    # 示例：
    #   ssh.InteractiveCMD("sudo systemctl restart nginx", [
    #       {"expect": r"\[sudo\].*password", "send": "密码"},
    #       {"expect": "Do you want to continue", "send": "y"},
    #   ])
    def InteractiveCMD(self, command, interactions=None, timeout=None, get_pty=True):
        try:
            blocked = self._check_and_block(command)
            if blocked:
                return blocked
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            timeout = timeout if timeout is not None else self.command_timeout
            session = SSHInteractiveSession(self.ssh_obj, command, get_pty=get_pty,
                                            timeout=timeout, encoding=self.encoding)
            steps = []
            timed_out = False
            exit_status = None
            try:
                for idx, item in enumerate(interactions or [], start=1):
                    expect, send = item.get("expect", ""), item.get("send", "")
                    wait = item.get("timeout", timeout)
                    if expect:
                        try:
                            session.expect(expect, timeout=wait)
                            steps.append({"step": idx, "expect": expect, "matched": True})
                        except TimeoutError:
                            steps.append({"step": idx, "expect": expect, "matched": False})
                            timed_out = True
                            break
                    if not timed_out and send:
                        session.send(send)
                if not timed_out:
                    exit_status = session.wait_exit(timeout)
                    session.read(1)
            finally:
                session.close()
            if timed_out:
                msg, code = "交互命令执行超时（未等到后续提示）", 500
            elif exit_status is None:
                msg, code = "命令执行超时或未正常结束（未取得退出码）", 500
            elif exit_status == 0:
                msg, code = "交互命令执行成功", 200
            else:
                msg, code = f"交互命令执行失败（退出码 {exit_status}）", 500
            return json.dumps({
                "code": code, "msg": msg,
                "data": session.get_output(),
                "exit_status": exit_status,
                "timed_out": timed_out,
                "steps": steps,
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"交互命令执行失败：{e}",
                               "data": None, "exit_status": None,
                               "timed_out": False, "steps": []}, ensure_ascii=False)

    # ==================== SFTP 文件操作 ====================

    # 将远程文件下载到本地
    def GetRemoteFile(self, remote_path, local_path):
        try:
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            self.sftp_obj.get(remote_path, local_path)
            return json.dumps({"code": 200, "msg": f"远程文件下载成功：{remote_path} -> {local_path}",
                               "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"远程文件下载失败：{e}", "data": False}, ensure_ascii=False)

    # 将本地文件上传到远程
    def PutLocalFile(self, localpath, remotepath):
        try:
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            self.sftp_obj.put(localpath, remotepath)
            return json.dumps({"code": 200, "msg": f"本地文件上传成功：{localpath} -> {remotepath}",
                               "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"本地文件上传失败：{e}", "data": False}, ensure_ascii=False)

    # 读取远程服务器文件的全部内容（paramiko 的 open() 不支持 encoding 参数，
    # 这里以二进制读取后自行解码，兼容 paramiko 3.x / 5.x）
    def read_remote_file(self, remote_file_path, encoding=None):
        try:
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            with self.sftp_obj.open(remote_file_path, mode="rb") as f:
                content = f.read()
            text = self._decode(content, encoding)
            return json.dumps({"code": 200, "msg": f"远程文件读取成功：{remote_file_path}",
                               "data": text}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"远程文件读取失败：{e}", "data": None}, ensure_ascii=False)

    # 将内容写入/保存到远程服务器文件
    # mode 支持：w=覆盖写、a=追加、wb=覆盖写(二进制)、ab=追加(二进制)
    def write_remote_file(self, remote_file_path, content, mode="w", encoding=None):
        try:
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            encoding = encoding or self.encoding
            if mode in ("w", "a"):
                bin_mode = "wb" if mode == "w" else "ab"
                data = content if isinstance(content, bytes) else content.encode(encoding)
                with self.sftp_obj.open(remote_file_path, mode=bin_mode) as f:
                    f.write(data)
            elif mode in ("wb", "ab"):
                data = content if isinstance(content, bytes) else content.encode(encoding)
                with self.sftp_obj.open(remote_file_path, mode=mode) as f:
                    f.write(data)
            else:
                return json.dumps({"code": 500, "msg": f"不支持的写入模式：{mode}",
                                   "data": False}, ensure_ascii=False)
            return json.dumps({"code": 200, "msg": f"内容写入远程文件成功：{remote_file_path}",
                               "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"内容写入远程文件失败：{e}", "data": False}, ensure_ascii=False)

    # 列出远程目录内容
    def ListRemoteDir(self, remote_path="."):
        try:
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            items = self.sftp_obj.listdir_attr(remote_path)
            data = [{
                "name": it.filename,
                "size": it.st_size,
                "mtime": it.st_mtime,
                "is_dir": bool(S_ISDIR(it.st_mode)),
            } for it in items]
            return json.dumps({"code": 200, "msg": f"目录 {remote_path} 共 {len(data)} 项",
                               "data": data}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"目录列表获取失败：{e}", "data": None}, ensure_ascii=False)

    # 远程创建目录
    def MakeRemoteDir(self, remote_path):
        try:
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            self.sftp_obj.mkdir(remote_path)
            return json.dumps({"code": 200, "msg": f"远程目录创建成功：{remote_path}",
                               "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"远程目录创建失败：{e}", "data": False}, ensure_ascii=False)

    # 删除远程文件
    def RemoveRemoteFile(self, remote_path):
        try:
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            self.sftp_obj.remove(remote_path)
            return json.dumps({"code": 200, "msg": f"远程文件删除成功：{remote_path}",
                               "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"远程文件删除失败：{e}", "data": False}, ensure_ascii=False)

    # 删除远程空目录
    def RemoveRemoteDir(self, remote_path):
        try:
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            self.sftp_obj.rmdir(remote_path)
            return json.dumps({"code": 200, "msg": f"远程目录删除成功：{remote_path}",
                               "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"远程目录删除失败：{e}", "data": False}, ensure_ascii=False)

    # 查看远程文件/目录属性
    def StatRemoteFile(self, remote_path):
        try:
            err_json = self._ensure_connected()
            if err_json:
                return err_json
            st = self.sftp_obj.stat(remote_path)
            data = {"name": remote_path, "size": st.st_size, "mtime": st.st_mtime,
                    "is_dir": bool(S_ISDIR(st.st_mode))}
            return json.dumps({"code": 200, "msg": f"远程路径属性获取成功：{remote_path}",
                               "data": data}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"远程路径属性获取失败：{e}", "data": None}, ensure_ascii=False)
