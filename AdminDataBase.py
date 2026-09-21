import os
import sqlite3
import csv
import datetime
import json
import socket
import time
from uuid import uuid4


class AdminDataBase(object):
    def __init__(self, database_path):
        self.database_path = database_path
        # 初始化时检查数据库文件目录是否存在，不存在则创建
        db_dir = os.path.dirname(self.database_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

    # 创建数据库连接（统一开启外键约束 + 防中文乱码）
    def _get_conn_cursor(self):
        conn = sqlite3.connect(self.database_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA encoding = 'UTF-8'")
        cursor = conn.cursor()
        return conn, cursor

    # ==================== 初始化 ====================

    # 初始化数据库（创建表 + 插入默认数据）
    def InitDatabase(self):
        conn, cursor = self._get_conn_cursor()
        try:
            # 1. 创建主机表（HostList 对应）
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS hosts (
                uuid TEXT PRIMARY KEY,
                address TEXT NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                port TEXT NOT NULL
            )
            ''')

            # 2. 创建主机组表（HostGroup 对应）
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                group_name TEXT PRIMARY KEY
            )
            ''')

            # 3. 创建组-主机关联表（多对多关系）
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_members (
                group_name TEXT NOT NULL,
                uuid TEXT NOT NULL,
                FOREIGN KEY (group_name) REFERENCES groups (group_name) ON DELETE CASCADE,
                FOREIGN KEY (uuid) REFERENCES hosts (uuid) ON DELETE CASCADE,
                PRIMARY KEY (group_name, uuid)  -- 避免重复关联
            )
            ''')

            # 插入默认数据（复刻原 JSON 的初始数据）
            cursor.execute(
                '''INSERT OR IGNORE INTO hosts (uuid, address, username, password, port) VALUES (?, ?, ?, ?, ?)''',
                ("1000", "127.0.0.1", "username", "password", "22"))
            cursor.execute('''INSERT OR IGNORE INTO groups (group_name) VALUES (?)''', ("DefaultGroup",))
            cursor.execute('''INSERT OR IGNORE INTO group_members (group_name, uuid) VALUES (?, ?)''',
                           ("DefaultGroup", "1000"))

            conn.commit()
            return {
                "code": 1,
                "msg": f"{self.database_path} 数据库结构初始化成功，已插入默认主机/组数据",
                "data": {"db_path": self.database_path}
            }
        except Exception as e:
            return {
                "code": 0,
                "msg": f"数据库初始化失败: {str(e)}",
                "data": {}
            }
        finally:
            conn.close()

    # ==================== 主机管理 ====================

    # 显示所有主机列表（数据存入data）
    def ShowHostList(self):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT uuid, address, username, password, port FROM hosts")
        hosts = cursor.fetchall()
        conn.close()
        # 格式化数据为列表字典，方便解析
        host_list = [
            {
                "uuid": host[0],
                "address": host[1],
                "username": host[2],
                "password": host[3],
                "port": host[4]
            } for host in hosts
        ]
        return {
            "code": 1,
            "msg": f"共查询到 {len(host_list)} 台主机",
            "data": host_list
        }

    # 添加新主机（uuid 为空时自动生成；自动校验地址、用户名与端口格式）
    def AddHost(self, uuid=None, address="", username="", password="", port="22"):
        if not address or not username:
            return {"code": 0, "msg": "主机地址与登录用户不能为空", "data": {}}
        try:
            port = str(int(port))
        except (ValueError, TypeError):
            return {"code": 0, "msg": f"端口号非法：{port}", "data": {}}
        if not uuid:
            uuid = uuid4().hex
        conn, cursor = self._get_conn_cursor()
        try:
            cursor.execute('''INSERT INTO hosts (uuid, address, username, password, port) VALUES (?, ?, ?, ?, ?)''',
                           (uuid, address, username, password, port))
            conn.commit()
            return {
                "code": 1,
                "msg": f"UUID {uuid} 添加成功",
                "data": {"uuid": uuid, "address": address}
            }
        except sqlite3.IntegrityError:
            return {
                "code": 0,
                "msg": f"UUID {uuid} 已存在，添加失败",
                "data": {}
            }
        finally:
            conn.close()

    # 全量修改主机
    def ModifyHost(self, uuid, modify_address, modify_username, modify_password, modify_port):
        try:
            modify_port = str(int(modify_port))
        except (ValueError, TypeError):
            return {"code": 0, "msg": f"端口号非法：{modify_port}", "data": {}}
        conn, cursor = self._get_conn_cursor()
        cursor.execute('''UPDATE hosts SET address=?, username=?, password=?, port=? WHERE uuid=?''',
                       (modify_address, modify_username, modify_password, modify_port, uuid))

        if cursor.rowcount > 0:
            conn.commit()
            res = {
                "code": 1,
                "msg": f"UUID {uuid} 全量信息修改完成",
                "data": {"uuid": uuid, "new_info": {"address": modify_address, "username": modify_username, "port": modify_port}}
            }
        else:
            res = {
                "code": 0,
                "msg": f"UUID {uuid} 不存在，修改失败",
                "data": {}
            }
        conn.close()
        return res

    # 单独修改主机IP地址
    def ModifyHostIp(self, uuid, new_address):
        """单独修改指定UUID主机的IP地址，其他信息不变"""
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT 1 FROM hosts WHERE uuid=?", (uuid,))
        if not cursor.fetchone():
            conn.close()
            return {
                "code": 0,
                "msg": f"UUID {uuid} 不存在，修改IP失败",
                "data": {}
            }
        cursor.execute('UPDATE hosts SET address=? WHERE uuid=?', (new_address, uuid))
        conn.commit()
        conn.close()
        return {
            "code": 1,
            "msg": f"UUID {uuid} 主机IP已修改为：{new_address}",
            "data": {"uuid": uuid, "new_ip": new_address}
        }

    # 单独修改主机账号 + 密码
    def ModifyHostUserPwd(self, uuid, new_username, new_password):
        """单独修改指定UUID主机的登录账号和密码，IP、端口不变"""
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT 1 FROM hosts WHERE uuid=?", (uuid,))
        if not cursor.fetchone():
            conn.close()
            return {
                "code": 0,
                "msg": f"UUID {uuid} 不存在，修改账号密码失败",
                "data": {}
            }
        cursor.execute('UPDATE hosts SET username=?, password=? WHERE uuid=?', (new_username, new_password, uuid))
        conn.commit()
        conn.close()
        return {
            "code": 1,
            "msg": f"UUID {uuid} 账号密码已更新",
            "data": {"uuid": uuid, "new_username": new_username}
        }

    # 单独修改主机端口号
    def ModifyHostPort(self, uuid, new_port):
        """单独修改指定UUID主机的端口号，其他信息不变"""
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT 1 FROM hosts WHERE uuid=?", (uuid,))
        if not cursor.fetchone():
            conn.close()
            return {
                "code": 0,
                "msg": f"UUID {uuid} 不存在，修改端口失败",
                "data": {}
            }
        cursor.execute('UPDATE hosts SET port=? WHERE uuid=?', (new_port, uuid))
        conn.commit()
        conn.close()
        return {
            "code": 1,
            "msg": f"UUID {uuid} 主机端口已修改为：{new_port}",
            "data": {"uuid": uuid, "new_port": new_port}
        }

    # 删除主机
    def DeleteHost(self, uuid):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT address FROM hosts WHERE uuid=?", (uuid,))
        host = cursor.fetchone()
        if not host:
            conn.close()
            return {
                "code": 0,
                "msg": f"UUID {uuid} 不存在，删除失败",
                "data": {}
            }
        cursor.execute("DELETE FROM hosts WHERE uuid=?", (uuid,))
        conn.commit()
        conn.close()
        return {
            "code": 1,
            "msg": f"UUID {uuid} 主机: {host[0]} 已被移除",
            "data": {"uuid": uuid, "address": host[0]}
        }

    # 批量删除主机
    def BatchDeleteHost(self, uuid_list):
        conn, cursor = self._get_conn_cursor()
        success_count, fail_count = 0, 0
        data_info = {"success_uuid": [], "fail_uuid": []}
        for uuid in uuid_list:
            cursor.execute("SELECT address FROM hosts WHERE uuid=?", (uuid,))
            host = cursor.fetchone()
            if not host:
                data_info["fail_uuid"].append(uuid)
                fail_count += 1
                continue
            cursor.execute("DELETE FROM hosts WHERE uuid=?", (uuid,))
            data_info["success_uuid"].append(uuid)
            success_count += 1
        conn.commit()
        conn.close()
        return {
            "code": 1 if success_count > 0 else 0,
            "msg": f"批量删除完成：成功 {success_count} 台，失败 {fail_count} 台",
            "data": data_info
        }

    # ==================== 查询 ====================

    # 按UUID精准查询主机
    def SearchHostByUUID(self, uuid):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT uuid, address, username, password, port FROM hosts WHERE uuid=?", (uuid,))
        host = cursor.fetchone()
        conn.close()
        if host:
            host_data = {"uuid": host[0], "address": host[1], "username": host[2], "password": host[3], "port": host[4]}
            return {"code": 1, "msg": f"查询到UUID {uuid} 的主机信息", "data": host_data}
        else:
            return {"code": 0, "msg": f"未查询到UUID {uuid} 的主机", "data": {}}

    # 按地址精准查询主机
    def SearchHostByAddress(self, address):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT uuid, address, username, password, port FROM hosts WHERE address=?", (address,))
        host = cursor.fetchone()
        conn.close()
        if host:
            host_data = {"uuid": host[0], "address": host[1], "username": host[2], "password": host[3], "port": host[4]}
            return {"code": 1, "msg": f"查询到地址 {address} 的主机信息", "data": host_data}
        else:
            return {"code": 0, "msg": f"未查询到地址为 {address} 的主机", "data": {}}

    # 模糊查询主机
    def FuzzySearchHost(self, keyword):
        conn, cursor = self._get_conn_cursor()
        sql = "SELECT uuid, address, username, password, port FROM hosts WHERE address LIKE ? OR username LIKE ?"
        cursor.execute(sql, (f"%{keyword}%", f"%{keyword}%"))
        hosts = cursor.fetchall()
        conn.close()
        host_list = [{"uuid": h[0], "address": h[1], "username": h[2], "password": h[3], "port": h[4]} for h in hosts]
        if hosts:
            return {"code": 1, "msg": f"共查询到 {len(host_list)} 台匹配主机", "data": host_list}
        else:
            return {"code": 0, "msg": f"未查询到包含关键词「{keyword}」的主机", "data": {}}

    # 校验主机是否存在
    def CheckHostExist(self, uuid):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT 1 FROM hosts WHERE uuid=?", (uuid,))
        res = True if cursor.fetchone() else False
        conn.close()
        return {"code": 1, "msg": "校验完成", "data": {"uuid": uuid, "exist": res}}

    # 校验组是否存在
    def CheckGroupExist(self, group_name):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT 1 FROM groups WHERE group_name=?", (group_name,))
        res = True if cursor.fetchone() else False
        conn.close()
        return {"code": 1, "msg": "校验完成", "data": {"group_name": group_name, "exist": res}}

    # 主机/组数量统计
    def GetHostCount(self):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT COUNT(*) FROM hosts")
        n = cursor.fetchone()[0]
        conn.close()
        return {"code": 1, "msg": f"当前共 {n} 台主机", "data": {"host_count": n}}

    def GetGroupCount(self):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT COUNT(*) FROM groups")
        n = cursor.fetchone()[0]
        conn.close()
        return {"code": 1, "msg": f"当前共 {n} 个主机组", "data": {"group_count": n}}

    # ==================== 主机组管理 ====================

    # 添加主机组
    def AddHostGroup(self, add_group_name):
        conn, cursor = self._get_conn_cursor()
        try:
            cursor.execute('''INSERT INTO groups (group_name) VALUES (?)''', (add_group_name,))
            conn.commit()
            return {
                "code": 1,
                "msg": f"主机组 {add_group_name} 已添加",
                "data": {"group_name": add_group_name}
            }
        except sqlite3.IntegrityError:
            return {
                "code": 0,
                "msg": f"{add_group_name} 组已存在，无法添加",
                "data": {}
            }
        finally:
            conn.close()

    # 删除主机组
    def DeleteHostGroup(self, delete_group_name):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("DELETE FROM groups WHERE group_name=?", (delete_group_name,))
        if cursor.rowcount > 0:
            conn.commit()
            res = {
                "code": 1,
                "msg": f"主机组 {delete_group_name} 已移除",
                "data": {"group_name": delete_group_name}
            }
        else:
            res = {
                "code": 0,
                "msg": f"主机组 {delete_group_name} 不存在，删除失败",
                "data": {}
            }
        conn.close()
        return res

    # 重命名主机组
    def RenameHostGroup(self, old_name, new_name):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT 1 FROM groups WHERE group_name=?", (old_name,))
        if not cursor.fetchone():
            conn.close()
            return {"code": 0, "msg": f"原组名 {old_name} 不存在", "data": {}}
        cursor.execute("SELECT 1 FROM groups WHERE group_name=?", (new_name,))
        if cursor.fetchone():
            conn.close()
            return {"code": 0, "msg": f"新组名 {new_name} 已存在，无法重命名", "data": {}}
        try:
            # 外键开启时直接改父表主键会触发 FOREIGN KEY constraint failed，
            # 在事务内延迟外键校验到提交时，保证两步更新原子完成
            conn.execute("BEGIN")
            conn.execute("PRAGMA defer_foreign_keys = ON")
            cursor.execute("UPDATE groups SET group_name=? WHERE group_name=?", (new_name, old_name))
            cursor.execute("UPDATE group_members SET group_name=? WHERE group_name=?", (new_name, old_name))
            conn.commit()
            return {"code": 1, "msg": f"主机组 {old_name} 已重命名为 {new_name}",
                    "data": {"old_name": old_name, "new_name": new_name}}
        except Exception as e:
            return {"code": 0, "msg": f"组重命名失败: {str(e)}", "data": {}}
        finally:
            conn.close()

    # 向指定组添加主机UUID
    def AddHostGroupOnUUID(self, group_name, uuid):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT 1 FROM groups WHERE group_name=?", (group_name,))
        if not cursor.fetchone():
            conn.close()
            return {"code": 0, "msg": f"主机组 {group_name} 不存在,添加失败", "data": {}}

        cursor.execute("SELECT 1 FROM hosts WHERE uuid=?", (uuid,))
        if not cursor.fetchone():
            conn.close()
            return {"code": 0, "msg": f"UUID {uuid} 不存在,添加失败", "data": {}}

        try:
            cursor.execute('''INSERT INTO group_members (group_name, uuid) VALUES (?, ?)''', (group_name, uuid))
            conn.commit()
            return {"code": 1, "msg": f"向主机组 {group_name} 增加UUID {uuid} 完成",
                    "data": {"group_name": group_name, "uuid": uuid}}
        except sqlite3.IntegrityError:
            return {"code": 0, "msg": f"UUID {uuid} 已存在于 {group_name} 组,添加失败", "data": {}}
        finally:
            conn.close()

    # 从指定组删除主机UUID
    def DeleteHostGroupOnUUID(self, group_name, uuid):
        conn, cursor = self._get_conn_cursor()
        cursor.execute('''SELECT 1 FROM group_members WHERE group_name=? AND uuid=?''', (group_name, uuid))
        if not cursor.fetchone():
            conn.close()
            return {"code": 0, "msg": f"UUID {uuid} 不存在于 {group_name} 组,删除失败", "data": {}}

        cursor.execute('''DELETE FROM group_members WHERE group_name=? AND uuid=?''', (group_name, uuid))
        conn.commit()
        conn.close()
        return {"code": 1, "msg": f"从主机组 {group_name} 移除UUID {uuid} 完成",
                "data": {"group_name": group_name, "uuid": uuid}}

    # 批量向组添加主机
    def BatchAddHost2Group(self, group_name, uuid_list):
        conn, cursor = self._get_conn_cursor()
        group_check = self.CheckGroupExist(group_name)
        if not group_check["data"]["exist"]:
            conn.close()
            return {
                "code": 0,
                "msg": f"主机组 {group_name} 不存在，批量添加失败",
                "data": {"group_name": group_name}
            }

        success_count, fail_count = 0, 0
        fail_msg = []
        for uuid in uuid_list:
            # 校验当前遍历的主机是否存在
            host_check = self.CheckHostExist(uuid)
            if not host_check["data"]["exist"]:
                fail_msg.append(f"UUID {uuid} 不存在，跳过添加")
                fail_count += 1
                continue
            try:
                # 向组-主机关联表插入数据
                cursor.execute(
                    'INSERT INTO group_members (group_name, uuid) VALUES (?, ?)',
                    (group_name, uuid)
                )
                success_count += 1
            except sqlite3.IntegrityError:
                # 捕获主机已在组内的重复异常
                fail_msg.append(f"UUID {uuid} 已存在于 {group_name} 组中，无需重复添加")
                fail_count += 1
        conn.commit()
        conn.close()
        return {
            "code": 1 if success_count > 0 else 0,
            "msg": f"批量添加完成：成功 {success_count} 台，失败 {fail_count} 台",
            "data": {
                "group_name": group_name,
                "success_count": success_count,
                "fail_count": fail_count,
                "fail_msg": fail_msg,
                "total_count": len(uuid_list)
            }
        }

    # 批量从组删除主机
    def BatchDelHostFromGroup(self, group_name, uuid_list):
        conn, cursor = self._get_conn_cursor()
        if not self.CheckGroupExist(group_name)["data"]["exist"]:
            conn.close()
            return {"code": 0, "msg": f"主机组 {group_name} 不存在", "data": {}}
        success_count, fail_count = 0, 0
        fail_msg = []
        for uuid in uuid_list:
            cursor.execute('SELECT 1 FROM group_members WHERE group_name=? AND uuid=?', (group_name, uuid))
            if not cursor.fetchone():
                fail_msg.append(f"UUID {uuid} 不在 {group_name} 组中")
                fail_count += 1
                continue
            cursor.execute('DELETE FROM group_members WHERE group_name=? AND uuid=?', (group_name, uuid))
            success_count += 1
        conn.commit()
        conn.close()
        return {
            "code": 1 if success_count > 0 else 0,
            "msg": f"批量移除完成：成功 {success_count} 台，失败 {fail_count} 台",
            "data": {"group_name": group_name, "success_count": success_count,
                     "fail_count": fail_count, "fail_msg": fail_msg}
        }

    # 清空组内主机
    def ClearGroupHosts(self, group_name):
        conn, cursor = self._get_conn_cursor()
        if not self.CheckGroupExist(group_name)["data"]["exist"]:
            conn.close()
            return {"code": 0, "msg": f"主机组 {group_name} 不存在", "data": {}}
        cursor.execute('DELETE FROM group_members WHERE group_name=?', (group_name,))
        conn.commit()
        conn.close()
        return {"code": 1, "msg": f"主机组 {group_name} 内所有主机已清空（组本身保留）",
                "data": {"group_name": group_name}}

    # 显示所有组及组内主机详情
    def ShowAllGroup(self):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT group_name FROM groups")
        groups = cursor.fetchall()
        group_data = []
        for (group_name,) in groups:
            cursor.execute('''SELECT h.uuid, h.address, h.username, h.password, h.port
                              FROM hosts h JOIN group_members gm ON h.uuid = gm.uuid WHERE gm.group_name=?''',
                           (group_name,))
            hosts = cursor.fetchall()
            host_item = [{"uuid": h[0], "address": h[1], "username": h[2], "password": h[3], "port": h[4]} for h in hosts]
            group_data.append({"group_name": group_name, "hosts": host_item})
        conn.close()
        return {"code": 1, "msg": f"共查询到 {len(group_data)} 个主机组", "data": group_data}

    # 显示所有组（含主机数、UUID列表）
    def ShowGroup(self):
        conn, cursor = self._get_conn_cursor()
        cursor.execute('''SELECT g.group_name, COUNT(gm.uuid) as host_count, GROUP_CONCAT(gm.uuid, ',') as uuid_list
                          FROM groups g LEFT JOIN group_members gm ON g.group_name = gm.group_name GROUP BY g.group_name''')
        groups = cursor.fetchall()
        group_list = []
        for group in groups:
            group_name, host_count, uuid_list = group
            group_list.append({
                "group_name": group_name,
                "host_count": int(host_count),
                "uuid_list": uuid_list.split(",") if uuid_list else []
            })
        conn.close()
        return {"code": 1, "msg": f"共查询到 {len(group_list)} 个主机组", "data": group_list}

    # 查询单个组详情
    def ShowSingleGroup(self, group_name):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT 1 FROM groups WHERE group_name=?", (group_name,))
        if not cursor.fetchone():
            conn.close()
            return {"code": 0, "msg": f"主机组 {group_name} 不存在", "data": {}}
        cursor.execute('''SELECT h.uuid, h.address, h.username, h.password, h.port
                          FROM hosts h JOIN group_members gm ON h.uuid = gm.uuid WHERE gm.group_name=?''', (group_name,))
        hosts = cursor.fetchall()
        host_list = [{"uuid": h[0], "address": h[1], "username": h[2], "password": h[3], "port": h[4]} for h in hosts]
        conn.close()
        return {"code": 1, "msg": f"查询到组 {group_name} 详情，共 {len(host_list)} 台主机",
                "data": {"group_name": group_name, "hosts": host_list}}

    # ==================== 连通性检测 ====================

    @staticmethod
    def _ping_host(address, username, port):
        """TCP 连通性检测（SSH 主机以目标端口连通为准），返回状态与延迟"""
        try:
            port = int(port)
        except (ValueError, TypeError):
            port = 22
        start = time.time()
        try:
            with socket.create_connection((address, port), timeout=2):
                latency = round((time.time() - start) * 1000, 1)
            return {"address": address, "username": username, "port": port,
                    "status": "ok", "latency_ms": latency}
        except Exception:
            return {"address": address, "username": username, "port": port,
                    "status": "fail", "latency_ms": None}

    # 组内主机Ping测试（真实 TCP 连通检测）
    def PingGroup(self, group_name):
        conn, cursor = self._get_conn_cursor()
        cursor.execute('''SELECT h.address, h.username, h.port FROM hosts h
                          JOIN group_members gm ON h.uuid = gm.uuid WHERE gm.group_name=?''', (group_name,))
        hosts = cursor.fetchall()
        conn.close()
        if not hosts:
            return {"code": 0, "msg": f"主机组 {group_name} 不存在或无主机", "data": {}}
        ping_data = [self._ping_host(h[0], h[1], h[2]) for h in hosts]
        ok_count = sum(1 for p in ping_data if p["status"] == "ok")
        return {"code": 1, "msg": f"组 {group_name} Ping测试完成（成功 {ok_count}/{len(ping_data)}）",
                "data": ping_data}

    # 全局Ping所有主机（真实 TCP 连通检测）
    def PingAllHosts(self):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT address, username, port FROM hosts")
        hosts = cursor.fetchall()
        conn.close()
        if not hosts:
            return {"code": 0, "msg": "暂无主机数据", "data": {}}
        ping_data = [self._ping_host(h[0], h[1], h[2]) for h in hosts]
        ok_count = sum(1 for p in ping_data if p["status"] == "ok")
        return {"code": 1, "msg": f"全局所有主机Ping测试完成（成功 {ok_count}/{len(ping_data)}）",
                "data": ping_data}

    # ==================== 备份 / 恢复 / 导入 / 导出 ====================

    # 数据库备份
    def BackupDatabase(self, backup_path=None):
        if not backup_path:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{self.database_path}_{timestamp}.sql"
        conn, cursor = self._get_conn_cursor()
        try:
            with open(backup_path, 'w', encoding='utf-8') as f:
                for line in conn.iterdump():
                    f.write(line + '\n')
            return {"code": 1, "msg": "数据库备份成功", "data": {"backup_path": backup_path}}
        except Exception as e:
            return {"code": 0, "msg": f"数据库备份失败: {str(e)}", "data": {}}
        finally:
            conn.close()

    # 数据库恢复
    def RestoreDatabase(self, sql_path):
        if not os.path.exists(sql_path):
            return {"code": 0, "msg": f"备份文件 {sql_path} 不存在", "data": {}}
        conn, cursor = self._get_conn_cursor()
        try:
            # 备份文件按字母序建表（group_members 先于 hosts/groups），
            # 外键校验开启时建表会因引用表不存在而报错，恢复期间临时关闭
            conn.execute("PRAGMA foreign_keys = OFF")
            with open(sql_path, 'r', encoding='utf-8') as f:
                sql_content = f.read()
                conn.executescript(sql_content)
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
            return {"code": 1, "msg": f"从 {sql_path} 恢复数据库成功", "data": {"restore_path": sql_path}}
        except Exception as e:
            return {"code": 0, "msg": f"数据库恢复失败: {str(e)}", "data": {}}
        finally:
            conn.close()

    # 导出主机到CSV（UTF-8 BOM，Excel 可直接打开）
    def ExportHosts2CSV(self, csv_path="hosts_export.csv"):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT uuid, address, username, password, port FROM hosts")
        hosts = cursor.fetchall()
        conn.close()
        try:
            with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(["UUID", "主机地址", "登录用户", "登录密码", "端口"])
                writer.writerows(hosts)
            return {"code": 1, "msg": "所有主机数据已导出到CSV",
                    "data": {"csv_path": csv_path, "host_count": len(hosts)}}
        except Exception as e:
            return {"code": 0, "msg": f"导出CSV失败: {str(e)}", "data": {}}

    # 导出主机到JSON
    def ExportHosts2JSON(self, json_path="hosts_export.json"):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("SELECT uuid, address, username, password, port FROM hosts")
        hosts = cursor.fetchall()
        conn.close()
        data = [{"uuid": r[0], "address": r[1], "username": r[2], "password": r[3], "port": r[4]} for r in hosts]
        try:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return {"code": 1, "msg": "所有主机数据已导出到JSON",
                    "data": {"json_path": json_path, "host_count": len(data)}}
        except Exception as e:
            return {"code": 0, "msg": f"导出JSON失败: {str(e)}", "data": {}}

    # 从CSV导入主机（列头兼容 ExportHosts2CSV 的导出格式）
    def ImportHostsFromCSV(self, csv_path):
        if not os.path.exists(csv_path):
            return {"code": 0, "msg": f"CSV文件 {csv_path} 不存在", "data": {}}
        conn, cursor = self._get_conn_cursor()
        try:
            with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    return {"code": 0, "msg": "CSV文件为空", "data": {}}
                col = {name.strip(): i for i, name in enumerate(header)}
                idx_uuid = col.get("UUID", 0)
                idx_addr = col.get("主机地址", 1)
                idx_user = col.get("登录用户", 2)
                idx_pwd = col.get("登录密码", 3)
                idx_port = col.get("端口", 4)
                success, fail = 0, 0
                for row in reader:
                    if len(row) < 5:
                        fail += 1
                        continue
                    try:
                        port = str(int(row[idx_port].strip()))
                        cursor.execute(
                            'INSERT OR IGNORE INTO hosts (uuid, address, username, password, port) VALUES (?, ?, ?, ?, ?)',
                            (row[idx_uuid].strip(), row[idx_addr].strip(),
                             row[idx_user].strip(), row[idx_pwd].strip(), port))
                        success += cursor.rowcount
                    except Exception:
                        fail += 1
                conn.commit()
            return {"code": 1 if success else 0,
                    "msg": f"导入完成：成功 {success} 台，跳过/失败 {fail} 行",
                    "data": {"csv_path": csv_path, "success_count": success, "fail_count": fail}}
        except Exception as e:
            return {"code": 0, "msg": f"导入CSV失败: {str(e)}", "data": {}}
        finally:
            conn.close()

    # ==================== 清空操作 ====================

    # 清空所有主机
    def ClearAllHosts(self):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("DELETE FROM hosts")
        conn.commit()
        conn.close()
        return {"code": 1, "msg": "所有主机已清空，主机组保留", "data": {}}

    # 清空所有组
    def ClearAllGroups(self):
        conn, cursor = self._get_conn_cursor()
        cursor.execute("DELETE FROM groups")
        conn.commit()
        conn.close()
        return {"code": 1, "msg": "所有主机组已清空，主机保留", "data": {}}
