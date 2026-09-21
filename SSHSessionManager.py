import datetime
import json
import os
import re
import threading
import time

from MySSHBase import MySSH


class SSHShellSession:
    """一个持久的交互式 SSH shell 会话（等价于一个终端窗体）"""

    def __init__(self, session_id, name, ssh, channel, own_connection=False):
        self.id = session_id
        self.name = name
        self.ssh = ssh                     # MySSH 实例（连接载体）
        self.channel = channel             # invoke_shell() 持久 shell 通道
        self.own_connection = own_connection  # 该连接是否由管理器创建（关闭时释放）
        self._buf = ""                     # 累计输出
        self._inbox = []                   # 收件箱（其他会话/管理器投递的消息）
        self.history = []                  # 命令历史（发送过的输入行）
        self._lock = threading.RLock()     # 保护通道读取（后台收取线程与手动读写共用）
        self.status = "connected"
        self.created = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_active = self.created

    # ---------- 基础能力 ----------

    def _decode(self, data):
        try:
            return data.decode(self.ssh.encoding)
        except (UnicodeDecodeError, LookupError):
            return data.decode("utf-8", errors="replace")

    def is_alive(self):
        """连接与通道是否仍然有效"""
        try:
            return (self.channel is not None and not self.channel.closed
                    and self.ssh.IsConnected())
        except Exception:
            return False

    def read(self, timeout=1):
        """轮询读取新到达的输出（累计进缓冲区），返回本次新增文本。
        线程安全：与后台收取线程通过 RLock 串行化通道读取。"""
        if self.status == "closed":
            return ""
        deadline = time.time() + timeout
        chunk = b""
        with self._lock:
            while time.time() < deadline:
                if self.channel.recv_ready():
                    data = self.channel.recv(65536)
                    if not data:                      # 远端关闭通道
                        self.status = "closed"
                        break
                    chunk += data
                elif self.channel.exit_status_ready():
                    while self.channel.recv_ready():
                        data = self.channel.recv(65536)
                        if not data:
                            break
                        chunk += data
                    self.status = "closed"
                    break
                else:
                    time.sleep(0.01)
        if chunk:
            text = self._decode(chunk)
            self._buf += text
            self.last_active = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return text
        return ""

    def send(self, text, newline=True):
        """向会话发送输入（默认补换行模拟回车），长文本自动分片发送，并记录命令历史"""
        if self.status == "closed":
            raise RuntimeError(f"会话 {self.id} 已关闭")
        if not isinstance(text, str):
            text = str(text)
        record = text.rstrip("\n")
        if newline and not text.endswith("\n"):
            text += "\n"
        data = text.encode(self.ssh.encoding)
        while data:
            n = self.channel.send(data)
            if n <= 0:
                break
            data = data[n:]
        if record:
            self.history.append(record)
        self.last_active = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def execute(self, command, wait=2.0):
        """在当前 shell 中执行一条命令：发送命令并轮询读取一段时间的输出"""
        self.send(command)
        return self.read(wait)

    def wait_prompt(self, pattern=r"[#$]\s*$", timeout=30):
        """等待输出中出现指定提示符（正则，默认匹配 # 或 $ 结尾），返回累计输出"""
        regex = re.compile(pattern)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if regex.search(self._buf):
                return self._buf
            if self.status == "closed":
                break
            self.read(0.5)
        raise TimeoutError(f"会话 {self.id} 等待提示「{pattern}」超时，已接收内容：{self._buf[-300:]}")

    def get_output(self, tail=None):
        """获取累计输出；tail 传行数时只返回末尾若干行"""
        if tail is None:
            return self._buf
        lines = self._buf.splitlines()
        return "\n".join(lines[-tail:])

    def get_history(self, tail=None):
        """获取命令历史；tail 传条数时只返回末尾若干条"""
        if tail is None:
            return list(self.history)
        return self.history[-tail:]

    def info(self):
        """会话信息快照（供 ListSessions 等使用）"""
        return {
            "id": self.id,
            "name": self.name,
            "host": self.ssh.address,
            "port": self.ssh.default_port,
            "user": self.ssh.username,
            "status": self.status,
            "created": self.created,
            "last_active": self.last_active,
            "output_tail": self.get_output(tail=5),
            "active": False,
        }

    def close(self):
        try:
            self.channel.close()
        except Exception:
            pass
        self.status = "closed"


class SSHSessionManager:
    """多 SSH 会话管理器：创建 / 遍历 / 切换 / 读写 / 执行 / 关闭"""

    def __init__(self):
        self.sessions = {}       # id -> SSHShellSession
        self.active_id = None    # 当前激活（切入）的会话
        self._seq = 0
        self._drainer = None             # 后台输出收取线程
        self._drainer_running = False

    # ---------- 内部工具 ----------

    def _new_id(self):
        while True:
            self._seq += 1
            sid = f"S{self._seq}"
            if sid not in self.sessions:
                return sid

    def _get_session(self, session_id):
        """按 id 取会话；id 为空时取当前激活会话。返回 (会话, 错误JSON或None)"""
        sid = session_id or self.active_id
        if sid is None:
            return None, json.dumps({"code": 500, "msg": "当前没有可用会话，请先 CreateSession 创建",
                                     "data": None}, ensure_ascii=False)
        sess = self.sessions.get(sid)
        if sess is None:
            return None, json.dumps({"code": 500, "msg": f"会话 {sid} 不存在",
                                     "data": None}, ensure_ascii=False)
        return sess, None

    # ---------- 后台输出收取（切出后命令持续执行、输出实时入缓冲区） ----------

    def StartDrainer(self):
        """启动后台收取线程：持续把所有连接会话的输出实时读入各自缓冲区。
        会话创建时自动启动，无需手动调用；重复调用幂等。"""
        if self._drainer and self._drainer.is_alive():
            return json.dumps({"code": 200, "msg": "后台收取线程已在运行", "data": True},
                              ensure_ascii=False)
        self._drainer_running = True
        self._drainer = threading.Thread(target=self._drain_loop, daemon=True,
                                         name="SSHSessionDrainer")
        self._drainer.start()
        return json.dumps({"code": 200, "msg": "后台收取线程已启动", "data": True},
                          ensure_ascii=False)

    def StopDrainer(self):
        """停止后台收取线程"""
        self._drainer_running = False
        if self._drainer and self._drainer.is_alive():
            self._drainer.join(timeout=1)
        self._drainer = None
        return json.dumps({"code": 200, "msg": "后台收取线程已停止", "data": True},
                          ensure_ascii=False)

    def _drain_loop(self):
        while self._drainer_running:
            for sess in list(self.sessions.values()):
                if sess.status == "connected":
                    try:
                        sess.read(0.02)
                    except Exception:
                        pass
            time.sleep(0.02)

    def __del__(self):
        try:
            self.StopDrainer()
        except Exception:
            pass

    # ---------- 会话生命周期 ----------

    def CreateSession(self, host, username, password, port=22, name=None,
                      ssh=None, connect_timeout=10):
        """新建一个交互式 shell 会话。
        - 传 ssh（已连接的 MySSH 实例）时复用其连接（关闭会话不释放连接）
        - 不传时内部新建连接（关闭会话时释放）
        返回 JSON，含新会话 id。"""
        conn_obj = None
        try:
            own_connection = False
            if ssh is None:
                ssh = MySSH(host, username, password, port,
                            connect_timeout=connect_timeout)
                conn_obj = ssh  # 记录自建连接，失败时兜底释放
                init_res = json.loads(ssh.Init())
                if init_res.get("code") != 200:
                    return json.dumps({"code": 500, "msg": f"新建会话连接失败：{init_res.get('msg')}",
                                       "data": None}, ensure_ascii=False)
                own_connection = True
            elif not ssh.IsConnected():
                return json.dumps({"code": 500, "msg": "传入的 MySSH 实例未连接，请先调用其 Init()",
                                   "data": None}, ensure_ascii=False)

            channel = ssh.ssh_obj.invoke_shell()
            sid = self._new_id()
            if not name:
                name = f"{host}:{port}#{sid}"
            sess = SSHShellSession(sid, name, ssh, channel, own_connection)
            sess.read(1)  # 先吸收登录横幅/首个提示符
            self.sessions[sid] = sess
            self.StartDrainer()  # 确保后台收取线程运行（幂等）
            if self.active_id is None:
                self.active_id = sid
            return json.dumps({"code": 200, "msg": f"会话 {sid} 创建成功（{name}）",
                               "data": sess.info()}, ensure_ascii=False)
        except Exception as e:
            # invoke_shell 等后续步骤失败时，释放本次自建的连接，避免泄漏
            if conn_obj is not None:
                try:
                    conn_obj.CloseSSH()
                except Exception:
                    pass
            return json.dumps({"code": 500, "msg": f"创建会话失败：{e}",
                               "data": None}, ensure_ascii=False)

    def CloseSession(self, session_id):
        """关闭指定会话（切出并销毁窗体）"""
        if session_id not in self.sessions:
            return json.dumps({"code": 500, "msg": f"会话 {session_id} 不存在",
                               "data": None}, ensure_ascii=False)
        sess = self.sessions.pop(session_id)
        try:
            sess.close()
            if sess.own_connection:
                sess.ssh.CloseSSH()
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"关闭会话 {session_id} 失败：{e}",
                               "data": None}, ensure_ascii=False)
        if self.active_id == session_id:
            self.active_id = next(iter(self.sessions), None)
        return json.dumps({"code": 200, "msg": f"会话 {session_id}（{sess.name}）已关闭",
                           "data": {"session_id": session_id}}, ensure_ascii=False)

    def CloseAll(self):
        """关闭并清空所有会话"""
        self.StopDrainer()
        closed = 0
        for sess in list(self.sessions.values()):
            try:
                sess.close()
                if sess.own_connection:
                    sess.ssh.CloseSSH()
                closed += 1
            except Exception:
                pass
        self.sessions.clear()
        self.active_id = None
        return json.dumps({"code": 200, "msg": f"已关闭全部 {closed} 个会话",
                           "data": {"closed_count": closed}}, ensure_ascii=False)

    # ---------- 遍历 / 切换 ----------

    def ListSessions(self):
        """遍历当前所有会话（含数量、激活会话、每个窗体的信息与最新输出）"""
        items = []
        for sid, sess in self.sessions.items():
            if sess.status == "connected":
                sess.read(0.2)  # 轻量刷新：更新状态与输出尾部
            info = sess.info()
            info["active"] = (sid == self.active_id)
            items.append(info)
        return json.dumps({"code": 200, "msg": f"当前共 {len(items)} 个会话",
                           "data": {"count": len(items), "active_id": self.active_id,
                                    "sessions": items}}, ensure_ascii=False)

    def GetSessionCount(self):
        return json.dumps({"code": 200, "msg": f"当前共 {len(self.sessions)} 个会话",
                           "data": {"count": len(self.sessions), "active_id": self.active_id}},
                          ensure_ascii=False)

    def GetSessionInfo(self, session_id=None):
        sess, err = self._get_session(session_id)
        if err:
            return err
        info = sess.info()
        info["active"] = (sess.id == self.active_id)
        return json.dumps({"code": 200, "msg": f"会话 {sess.id} 信息",
                           "data": info}, ensure_ascii=False)

    def SwitchSession(self, session_id):
        """切入指定会话（焦点切换；原会话切出后后台保留）"""
        sess = self.sessions.get(session_id)
        if sess is None:
            return json.dumps({"code": 500, "msg": f"会话 {session_id} 不存在",
                               "data": None}, ensure_ascii=False)
        if sess.status == "closed" or not sess.is_alive():
            return json.dumps({"code": 500, "msg": f"会话 {session_id} 已关闭，无法切入",
                               "data": None}, ensure_ascii=False)
        self.active_id = session_id
        info = sess.info()
        info["active"] = True
        return json.dumps({"code": 200, "msg": f"已切入会话 {session_id}（{info['name']}）",
                           "data": info}, ensure_ascii=False)

    def GetActiveSession(self):
        """获取当前激活会话信息"""
        sess, err = self._get_session(None)
        if err:
            return err
        info = sess.info()
        info["active"] = True
        return json.dumps({"code": 200, "msg": f"当前激活会话：{sess.id}",
                           "data": info}, ensure_ascii=False)

    # ---------- 自由读写 / 执行 ----------

    def Read(self, session_id=None, timeout=1):
        """读取指定会话（默认激活会话）自本次调用起新到达的输出。
        后台收取线程已实时把输出读入缓冲区，这里返回的是缓冲区新增部分。"""
        sess, err = self._get_session(session_id)
        if err:
            return err
        before = len(sess.get_output())
        sess.read(timeout)
        text = sess.get_output()[before:]
        return json.dumps({"code": 200, "msg": f"读取会话 {sess.id} 完成",
                           "data": {"session_id": sess.id, "text": text,
                                    "buffer": sess.get_output()}}, ensure_ascii=False)

    def Send(self, session_id=None, text=""):
        """向指定会话（默认激活会话）发送输入"""
        sess, err = self._get_session(session_id)
        if err:
            return err
        try:
            sess.send(text)
            return json.dumps({"code": 200, "msg": f"已向会话 {sess.id} 发送输入",
                               "data": {"session_id": sess.id, "sent": text}},
                              ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"发送失败：{e}",
                               "data": None}, ensure_ascii=False)

    def Execute(self, session_id=None, command="", wait=2.0):
        """在指定会话（默认激活会话）中执行命令并读取一段时间输出"""
        sess, err = self._get_session(session_id)
        if err:
            return err
        if not command.strip():
            return json.dumps({"code": 500, "msg": "命令不能为空",
                               "data": None}, ensure_ascii=False)
        try:
            # 与 MySSH 命令安全过滤保持一致：被黑/白名单拦截的命令不注入终端
            allow, rule = sess.ssh._check_command(command)
            if not allow:
                return json.dumps({"code": 403,
                                   "msg": f"命令被安全策略拦截（{sess.ssh.filter_mode}）「{rule}」：{command}",
                                   "data": None}, ensure_ascii=False)
            before = len(sess.get_output())
            sess.send(command)
            sess.read(wait)  # 等待/收取输出（后台收取线程也在同步读入缓冲区）
            text = sess.get_output()[before:]
            return json.dumps({"code": 200,
                               "msg": f"会话 {sess.id} 已执行命令（读取 {wait}s 输出）",
                               "data": {"session_id": sess.id, "command": command,
                                        "text": text, "buffer": sess.get_output()}},
                              ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"执行失败：{e}",
                               "data": None}, ensure_ascii=False)

    def WaitPrompt(self, session_id=None, pattern=r"[#$]\s*$", timeout=30):
        """等待指定会话输出中出现提示符（正则），用于确认命令执行完毕"""
        sess, err = self._get_session(session_id)
        if err:
            return err
        try:
            buf = sess.wait_prompt(pattern, timeout)
            return json.dumps({"code": 200, "msg": f"会话 {sess.id} 已等待到提示符",
                               "data": {"session_id": sess.id, "output": buf}},
                              ensure_ascii=False)
        except TimeoutError as e:
            return json.dumps({"code": 500, "msg": str(e),
                               "data": {"session_id": sess.id,
                                        "output": sess.get_output()}}, ensure_ascii=False)

    # ---------- 命令执行状态检测 ----------

    def GetCommandState(self, session_id=None, timeout=2, use_probe=True,
                        prompt_pattern=r"[#$]\s*$"):
        """检测目标窗体当前命令执行状态：idle（空闲/执行完毕）/ busy（正在执行）/ closed。
        原理：
          1) 提示符检测（无副作用）：输出尾部匹配提示符（默认 # 或 $ 结尾）→ 空闲；
          2) 探针检测：否则发送无害标记 echo __STxxx__，短时间回显 → 空闲；
             不回显 → 探针排在当前命令之后，说明命令正在执行。
        提示符与探针均不匹配时（无提示符终端）可设 use_probe=False 直接按 busy 处理。"""
        sess, err = self._get_session(session_id)
        if err:
            return err
        if sess.status == "closed" or not sess.is_alive():
            return json.dumps({"code": 200, "msg": f"会话 {sess.id} 已关闭",
                               "data": {"session_id": sess.id, "state": "closed",
                                        "method": "none"}}, ensure_ascii=False)
        # 1) 提示符检测
        sess.read(0.3)
        if re.search(prompt_pattern, sess.get_output()):
            return json.dumps({"code": 200, "msg": f"会话 {sess.id} 空闲（提示符就绪）",
                               "data": {"session_id": sess.id, "state": "idle",
                                        "method": "prompt"}}, ensure_ascii=False)
        if not use_probe:
            return json.dumps({"code": 200, "msg": f"会话 {sess.id} 未出现提示符，疑似正在执行",
                               "data": {"session_id": sess.id, "state": "busy",
                                        "method": "prompt"}}, ensure_ascii=False)
        # 2) 探针检测
        token = f"__ST{int(time.time() * 1000)}__"
        before = len(sess.get_output())
        try:
            sess.send(f"echo {token}")
        except Exception:
            return json.dumps({"code": 200, "msg": f"会话 {sess.id} 已关闭",
                               "data": {"session_id": sess.id, "state": "closed",
                                        "method": "none"}}, ensure_ascii=False)
        # 关键：PTY 会立即回显敲入的 "echo 标记" 行（即使命令仍在执行），
        # 因此必须匹配「独立成行的标记」才是命令真正执行后的输出，回显行不算
        probe_re = re.compile(rf"(?m)^[ \t]*{re.escape(token)}[ \t]*\r?$")
        deadline = time.time() + timeout
        while time.time() < deadline:
            sess.read(0.2)
            if probe_re.search(sess.get_output()[before:]):
                return json.dumps({"code": 200, "msg": f"会话 {sess.id} 空闲（探针已回显）",
                                   "data": {"session_id": sess.id, "state": "idle",
                                            "method": "probe"}}, ensure_ascii=False)
        return json.dumps({"code": 200, "msg": f"会话 {sess.id} 正在执行（探针未回显）",
                           "data": {"session_id": sess.id, "state": "busy",
                                    "method": "probe"}}, ensure_ascii=False)

    def WaitCommandFinish(self, session_id=None, timeout=30, interval=1,
                          prompt_pattern=r"[#$]\s*$"):
        """等待目标窗体当前命令执行完毕（提示符重新出现即视为完毕）。
        超时返回 busy；会话关闭返回 closed。仅用提示符检测，不产生探针噪声。"""
        sess, err = self._get_session(session_id)
        if err:
            return err
        deadline = time.time() + timeout
        while time.time() < deadline:
            if sess.status == "closed" or not sess.is_alive():
                return json.dumps({"code": 500, "msg": f"会话 {sess.id} 已关闭",
                                   "data": {"session_id": sess.id,
                                            "state": "closed"}}, ensure_ascii=False)
            sess.read(interval)
            if re.search(prompt_pattern, sess.get_output()):
                return json.dumps({"code": 200, "msg": f"会话 {sess.id} 命令已执行完毕",
                                   "data": {"session_id": sess.id,
                                            "state": "idle"}}, ensure_ascii=False)
        return json.dumps({"code": 500, "msg": f"会话 {sess.id} 等待超时（{timeout}s），命令仍在执行",
                           "data": {"session_id": sess.id,
                                    "state": "busy"}}, ensure_ascii=False)

    # ---------- 会话间消息 ----------

    def PostMessage(self, to_session_id, message, from_id="manager"):
        """向指定会话投递一条消息（进入其收件箱，不干扰其终端输入）"""
        sess, err = self._get_session(to_session_id)
        if err:
            return err
        msg = {"from": from_id,
               "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "content": message}
        sess._inbox.append(msg)
        return json.dumps({"code": 200, "msg": f"已向会话 {sess.id} 投递消息",
                           "data": msg}, ensure_ascii=False)

    def ReadMessages(self, session_id=None, clear=True):
        """读取指定会话收件箱中的消息（默认读取后清空）"""
        sess, err = self._get_session(session_id)
        if err:
            return err
        msgs = list(sess._inbox)
        if clear:
            sess._inbox.clear()
        return json.dumps({"code": 200, "msg": f"会话 {sess.id} 收件箱共 {len(msgs)} 条消息",
                           "data": {"session_id": sess.id, "messages": msgs}},
                          ensure_ascii=False)

    def SendToSession(self, to_session_id, text, from_session_id=None):
        """把一个窗体的输入直接注入到另一个窗体（如同在目标窗体键入并回车执行）"""
        sess, err = self._get_session(to_session_id)
        if err:
            return err
        from_tag = from_session_id or "外部"
        try:
            sess.send(text)
            return json.dumps({"code": 200,
                               "msg": f"已将输入注入会话 {sess.id}（来自 {from_tag}）",
                               "data": {"from": from_tag, "to": sess.id, "sent": text}},
                              ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"注入失败：{e}",
                               "data": None}, ensure_ascii=False)

    def Broadcast(self, message, from_id="manager"):
        """向所有会话投递一条消息（收件箱方式，不干扰终端输入）"""
        msg = {"from": from_id,
               "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "content": message}
        sent = []
        for sid, sess in self.sessions.items():
            sess._inbox.append(msg)
            sent.append(sid)
        return json.dumps({"code": 200, "msg": f"已向 {len(sent)} 个会话广播消息",
                           "data": {"session_ids": sent, "message": msg}},
                          ensure_ascii=False)

    # ---------- 命令历史 / 会话持久化 ----------

    def GetHistory(self, session_id=None, tail=20):
        """获取指定会话的命令历史（默认最近 20 条）"""
        sess, err = self._get_session(session_id)
        if err:
            return err
        return json.dumps({"code": 200, "msg": f"会话 {sess.id} 命令历史共 {len(sess.history)} 条",
                           "data": {"session_id": sess.id, "history": sess.get_history(tail)}},
                          ensure_ascii=False)

    def ExportSessions(self, path=None, include_password=True):
        """将会话配置与命令历史持久化到 JSON 文件（重启后可恢复）。
        注意：include_password=True 时文件包含明文密码，请妥善保管。"""
        if not path:
            path = "sessions_backup.json"
        data = {
            "version": 1,
            "exported_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sessions": [
                {
                    "id": sess.id,
                    "name": sess.name,
                    "host": sess.ssh.address,
                    "port": sess.ssh.default_port,
                    "username": sess.ssh.username,
                    "password": sess.ssh.password if include_password else None,
                    "created": sess.created,
                    "history": sess.get_history(),
                } for sess in self.sessions.values()
            ],
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return json.dumps({"code": 200, "msg": f"已导出 {len(data['sessions'])} 个会话配置到 {path}",
                               "data": {"path": path, "session_count": len(data["sessions"])}},
                              ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"导出会话失败：{e}",
                               "data": None}, ensure_ascii=False)

    def RestoreSessions(self, path="sessions_backup.json", connect=True):
        """从持久化文件恢复会话。
        connect=True：重新连接并创建会话（恢复名称与命令历史），返回新会话 id；
        connect=False：仅解析校验备份文件（dry-run），不创建会话。"""
        if not os.path.exists(path):
            return json.dumps({"code": 500, "msg": f"会话备份文件 {path} 不存在",
                               "data": None}, ensure_ascii=False)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"读取备份文件失败：{e}",
                               "data": None}, ensure_ascii=False)

        restored, failed = [], []
        for item in data.get("sessions", []):
            original_id = item.get("id")
            name = item.get("name")
            host = item.get("host")
            username = item.get("username")
            password = item.get("password") or ""
            try:
                port = int(item.get("port", 22))
            except (ValueError, TypeError):
                port = 22
            try:
                if not connect:
                    restored.append({"original_id": original_id, "name": name,
                                     "host": host, "port": port,
                                     "history_count": len(item.get("history") or []),
                                     "connected": False})
                    continue
                res = json.loads(self.CreateSession(host, username, password,
                                                    port, name=name))
                if res.get("code") != 200:
                    failed.append({"original_id": original_id,
                                   "reason": res.get("msg")})
                    continue
                sid = res["data"]["id"]
                self.sessions[sid].history = list(item.get("history") or [])
                restored.append({"original_id": original_id, "id": sid, "name": name,
                                 "host": host, "port": port,
                                 "history_count": len(self.sessions[sid].history),
                                 "connected": True})
            except Exception as e:
                failed.append({"original_id": original_id, "reason": str(e)})

        return json.dumps({"code": 1 if restored else 0,
                           "msg": f"恢复完成：成功 {len(restored)} 个，失败 {len(failed)} 个",
                           "data": {"restored": restored, "failed": failed}},
                          ensure_ascii=False)

    # ---------- 高级：直接取会话对象 ----------

    def GetSession(self, session_id=None):
        """返回 SSHShellSession 对象（非 JSON），供高级用法直接操作底层会话"""
        sess, _ = self._get_session(session_id)
        return sess
