# -*- coding: utf-8 -*-
import paramiko
import json

class MySSH:
    def __init__(self, address, username, password, default_port):
        self.address = address
        self.default_port = default_port
        self.username = username
        self.password = password
        self.ssh_obj = None
        self.sftp_obj = None

    # 初始化SSH接口
    def Init(self):
        try:
            self.ssh_obj = paramiko.SSHClient()
            self.ssh_obj.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh_obj.connect(self.address, self.default_port, self.username, self.password,
                                 timeout=3, allow_agent=False, look_for_keys=False)
            self.sftp_obj = self.ssh_obj.open_sftp()
            return json.dumps({"code": 200, "msg": "SSH连接初始化成功", "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"SSH连接初始化失败：{str(e)}", "data": False}, ensure_ascii=False)

    # 执行命令并返回执行结果
    def BatchCMD(self, command):
        try:
            stdin, stdout, stderr = self.ssh_obj.exec_command(command, timeout=3)
            result = stdout.read()
            if len(result) != 0:
                result = str(result).replace("\\n", "\n")
                result = result.replace("b'", "").replace("'", "")
                return json.dumps({"code": 200, "msg": "命令执行成功，已获取结果", "data": result}, ensure_ascii=False)
            else:
                return json.dumps({"code": 500, "msg": "命令执行成功，但未获取到返回结果", "data": None}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"命令执行失败：{str(e)}", "data": None}, ensure_ascii=False)

    # 执行非交互命令 只返回执行状态
    def BatchCMD_NotRef(self, command):
        try:
            stdin, stdout, stderr = self.ssh_obj.exec_command(command, timeout=3)
            stdout.read()  # 阻塞确保命令执行完成
            return json.dumps({"code": 200, "msg": "非交互命令执行成功", "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"非交互命令执行失败：{str(e)}", "data": False}, ensure_ascii=False)

    # 将远程文件下载到本地
    def GetRemoteFile(self, remote_path, local_path):
        try:
            self.sftp_obj.get(remote_path, local_path)
            return json.dumps({"code": 200, "msg": f"远程文件下载成功：{remote_path} -> {local_path}", "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"远程文件下载失败：{str(e)}", "data": False}, ensure_ascii=False)

    # 将本地文件上传到远程
    def PutLocalFile(self, localpath, remotepath):
        try:
            self.sftp_obj.put(localpath, remotepath)
            return json.dumps({"code": 200, "msg": f"本地文件上传成功：{localpath} -> {remotepath}", "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"本地文件上传失败：{str(e)}", "data": False}, ensure_ascii=False)

    # 获取系统型号
    def GetSystemVersion(self):
        return self.BatchCMD("uname -a")

    # 关闭SSH接口
    def CloseSSH(self):
        try:
            if self.sftp_obj:
                self.sftp_obj.close()
            if self.ssh_obj:
                self.ssh_obj.close()
            return json.dumps({"code": 200, "msg": "SSH连接已成功关闭", "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"SSH连接关闭失败：{str(e)}", "data": False}, ensure_ascii=False)

    # 读取远程服务器文件的全部内容
    def read_remote_file(self, remote_file_path, encoding="utf-8"):
        try:
            with self.sftp_obj.open(remote_file_path, mode='r', encoding=encoding) as f:
                file_content = f.read()
            return json.dumps({"code": 200, "msg": f"远程文件读取成功：{remote_file_path}", "data": file_content}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"远程文件读取失败：{str(e)}", "data": None}, ensure_ascii=False)

    # 将内容写入/保存到远程服务器文件
    def write_remote_file(self, remote_file_path, content, mode="w", encoding="utf-8"):
        try:
            if mode in ("w", "a"):
                with self.sftp_obj.open(remote_file_path, mode=mode, encoding=encoding) as f:
                    f.write(content)
            elif mode in ("wb", "ab"):
                with self.sftp_obj.open(remote_file_path, mode=mode) as f:
                    f.write(content.encode(encoding))
            return json.dumps({"code": 200, "msg": f"内容写入远程文件成功：{remote_file_path}", "data": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"code": 500, "msg": f"内容写入远程文件失败：{str(e)}", "data": False}, ensure_ascii=False)