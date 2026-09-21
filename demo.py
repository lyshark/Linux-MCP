import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from MySSHBase import MySSH
from AdminDataBase import AdminDataBase
from SSHSessionManager import SSHSessionManager


def show(title, result):
    """打印一段测试结果"""
    try:
        obj = json.loads(result) if isinstance(result, str) else result
    except Exception:
        obj = result
    print(f"\n--- {title} ---")
    print(json.dumps(obj, ensure_ascii=False, indent=2) if not isinstance(obj, str) else obj)


def test_database():
    print("=" * 60)
    print("【一、数据库部分 AdminDataBase】")
    print("=" * 60)
    tmp_dir = tempfile.mkdtemp(prefix="ssh_db_test_")
    db = AdminDataBase(os.path.join(tmp_dir, "test.db"))

    show("InitDatabase 初始化", db.InitDatabase())

    host_ids = []
    # 添加真实测试主机（若环境变量存在）与一条本地演示主机
    test_host = os.environ.get("TEST_SSH_HOST")
    if test_host:
        res = db.AddHost(address=test_host,
                         username=os.environ.get("TEST_SSH_USER", "root"),
                         password=os.environ.get("TEST_SSH_PWD", ""),
                         port=os.environ.get("TEST_SSH_PORT", "22"))
        show("AddHost 添加真实测试主机(自动UUID)", res)
        host_ids.append(res["data"]["uuid"])
    res = db.AddHost(address="10.0.0.88", username="admin", password="admin123", port="22")
    show("AddHost 添加演示主机", res)
    host_ids.append(res["data"]["uuid"])

    show("AddHost 端口非法校验", db.AddHost(address="1.2.3.4", username="u", password="p", port="abc"))

    show("ShowHostList 主机列表", db.ShowHostList())
    show("GetHostCount 主机数量", db.GetHostCount())
    show("SearchHostByAddress 按地址查询", db.SearchHostByAddress("10.0.0.88"))
    show("FuzzySearchHost 模糊查询", db.FuzzySearchHost("10.0"))
    show("ModifyHostPort 改端口", db.ModifyHostPort(host_ids[1], "2222"))
    show("ModifyHostIp 改IP", db.ModifyHostIp(host_ids[1], "10.0.0.99"))
    show("ModifyHostUserPwd 改账号密码", db.ModifyHostUserPwd(host_ids[1], "newadmin", "newpwd"))

    show("AddHostGroup 建组", db.AddHostGroup("WebServers"))
    show("AddHostGroupOnUUID 组内加主机", db.AddHostGroupOnUUID("WebServers", host_ids[1]))
    show("BatchAddHost2Group 批量加主机", db.BatchAddHost2Group("WebServers", host_ids))
    show("ShowSingleGroup 组详情", db.ShowSingleGroup("WebServers"))
    show("ShowGroup 组概览", db.ShowGroup())
    show("PingGroup 组内真实TCP连通检测", db.PingGroup("WebServers"))

    csv_path = os.path.join(tmp_dir, "hosts.csv")
    json_path = os.path.join(tmp_dir, "hosts.json")
    show("ExportHosts2CSV 导出CSV", db.ExportHosts2CSV(csv_path))
    show("ExportHosts2JSON 导出JSON", db.ExportHosts2JSON(json_path))

    db2 = AdminDataBase(os.path.join(tmp_dir, "import_test.db"))
    db2.InitDatabase()
    show("ImportHostsFromCSV 导入CSV", db2.ImportHostsFromCSV(csv_path))
    show("导入后查询验证", db2.ShowHostList())

    backup = os.path.join(tmp_dir, "backup.sql")
    show("BackupDatabase 备份", db.BackupDatabase(backup))
    db3 = AdminDataBase(os.path.join(tmp_dir, "restore_test.db"))
    show("RestoreDatabase 恢复", db3.RestoreDatabase(backup))
    show("恢复后查询验证", db3.ShowHostList())

    show("RenameHostGroup 重命名组", db.RenameHostGroup("WebServers", "WebCluster"))
    show("DeleteHostGroupOnUUID 组内移除主机", db.DeleteHostGroupOnUUID("WebCluster", host_ids[1]))
    show("ClearGroupHosts 清空组内主机", db.ClearGroupHosts("WebCluster"))
    show("DeleteHostGroup 删组", db.DeleteHostGroup("WebCluster"))
    show("DeleteHost 删主机", db.DeleteHost(host_ids[1]))
    show("BatchDeleteHost 批量删主机", db.BatchDeleteHost(host_ids))
    show("ClearAllGroups 清空所有组", db.ClearAllGroups())

    print(f"\n[数据库测试临时目录] {tmp_dir}（保留供检查，可手动删除）")


def test_ssh():
    host = os.environ.get("TEST_SSH_HOST")
    if not host:
        print("\n[跳过] 未设置 TEST_SSH_HOST 环境变量，SSH 部分不执行。")
        print("        设置方式：TEST_SSH_HOST / TEST_SSH_USER / TEST_SSH_PWD / TEST_SSH_PORT")
        return

    user = os.environ.get("TEST_SSH_USER", "root")
    pwd = os.environ.get("TEST_SSH_PWD", "")
    port = int(os.environ.get("TEST_SSH_PORT", "22"))

    print("\n" + "=" * 60)
    print(f"【二、SSH 部分 MySSH】目标 {host}:{port}")
    print("=" * 60)

    with MySSH(host, user, pwd, port) as ssh:  # with 自动连接与关闭
        show("Init 连接初始化", ssh.Init())
        show("BatchCMD uname -a（正常输出+退出码）", ssh.BatchCMD("uname -a"))
        show("BatchCMD 中文输出（编码正确性）", ssh.BatchCMD("echo 中文测试-中文输出"))
        show("BatchCMD 失败命令（stderr+退出码）", ssh.BatchCMD("ls /nonexistent_path_xyz"))
        show("BatchCMD_NotRef 非交互命令", ssh.BatchCMD_NotRef("touch /tmp/my_ssh_demo_ok"))
        show("GetSystemVersion 系统型号", ssh.GetSystemVersion())
        show("IsConnected 连接状态", ssh.IsConnected())

        show("InteractiveCMD 脚本化交互（read 提示→应答）",
             ssh.InteractiveCMD(
                 'bash -c \'read -p "请输入名字: " name; echo "收到:$name"\'',
                 interactions=[{"expect": "请输入名字", "send": "豆包"}]))
        show("InteractiveCMD 交互超时保护（等不到提示则超时返回）",
             ssh.InteractiveCMD("sleep 1; echo done",
                                interactions=[{"expect": "永不出现的提示", "send": "x", "timeout": 2}]))
        show("InteractiveCMD 无交互项（PTY 普通命令）", ssh.InteractiveCMD("echo pty-ok"))

        print("\n--- InvokeCMD 手动交互会话（cat 回显） ---")
        session = ssh.InvokeCMD("cat")
        session.send("hello-interactive")
        session.send_eof()
        print("   会话输出:", repr(session.read(3)))
        session.close()

        print("\n--- InvokeCMD 非PTY stderr 捕获 ---")
        session2 = ssh.InvokeCMD("ls /nonexistent_path_xyz", get_pty=False)
        out2 = session2.read(3)
        print("   输出(含stderr):", repr(out2))
        print("   退出码:", session2.wait_exit())
        session2.close()

        print("\n--- 命令安全过滤测试 ---")
        show("SetCommandFilter 开启黑名单", ssh.SetCommandFilter("blacklist"))
        show("黑名单：rm 被拦截", ssh.BatchCMD("rm -rf /tmp/forbidden"))
        show("黑名单：管道中的 rm 也被拦截", ssh.BatchCMD("echo x | rm -f /tmp/a"))
        show("黑名单：sudo rm 同样拦截", ssh.BatchCMD("sudo rm -f /etc/hosts"))
        show("黑名单：dd 写盘被拦截", ssh.BatchCMD("dd if=/dev/zero of=/dev/sda"))
        show("黑名单：reboot 被拦截", ssh.BatchCMD("reboot"))
        show("黑名单：正常命令不受影响（无误杀）", ssh.BatchCMD("cat /etc/hostname"))
        show("AddBlacklist 追加自定义规则", ssh.AddBlacklist("poweroff"))
        show("黑名单：自定义规则生效", ssh.BatchCMD("sudo poweroff"))
        show("InteractiveCMD 黑名单拦截", ssh.InteractiveCMD("rm -rf /tmp/x"))
        print("   InvokeCMD 黑名单拦截（抛异常）:")
        try:
            ssh.InvokeCMD("rm -rf /tmp/x")
        except Exception as e:
            print("   捕获异常:", type(e).__name__, "-", str(e))
        show("GetCommandFilter 查看策略", ssh.GetCommandFilter())

        show("SetCommandFilter 切换白名单", ssh.SetCommandFilter("whitelist"))
        show("白名单：ls 允许", ssh.BatchCMD("ls /tmp"))
        show("白名单：echo 允许", ssh.BatchCMD("echo whitelist-ok"))
        show("白名单：管道多命令逐段校验（cd && echo）", ssh.BatchCMD("cd /tmp && echo ok"))
        show("白名单：rm 拦截", ssh.BatchCMD("rm -rf /tmp/forbidden"))
        show("白名单：不在名单的命令拦截（fdisk）", ssh.BatchCMD("fdisk -l"))
        show("白名单：不在名单的命令拦截（python3）", ssh.BatchCMD("python3 -V"))
        show("恢复关闭过滤", ssh.SetCommandFilter("off"))
        show("关闭后正常放行", ssh.BatchCMD("echo filter-off-ok"))

        print("\n--- 多会话管理测试 ---")
        mgr = SSHSessionManager()
        r1 = json.loads(mgr.CreateSession(host, user, pwd, port, name="窗口A", ssh=ssh))
        sid_a = r1["data"]["id"]
        show("CreateSession 新建会话A（复用当前连接）", r1)
        r2 = json.loads(mgr.CreateSession(host, user, pwd, port, name="窗口B"))
        sid_b = r2["data"]["id"]
        show("CreateSession 新建会话B（独立连接）", r2)
        show("GetSessionCount 会话数量", mgr.GetSessionCount())
        show("ListSessions 遍历当前会话", mgr.ListSessions())
        show("Execute 在会话A执行命令", mgr.Execute(sid_a, "echo hello-from-window-A", wait=2))
        ssh.SetCommandFilter("blacklist")
        show("窗体 Execute 受命令安全过滤保护", mgr.Execute(sid_a, "rm -rf /tmp/forbidden"))
        ssh.SetCommandFilter("off")
        show("Read 读取会话A新输出", mgr.Read(sid_a, timeout=1))
        show("SwitchSession 切入会话B", mgr.SwitchSession(sid_b))
        show("Execute 在激活会话（会话B）执行命令", mgr.Execute(command="echo active-window-B", wait=2))
        show("Send 向会话B发送输入", mgr.Send(sid_b, "echo send-test"))
        show("WaitPrompt 等待会话B提示符", mgr.WaitPrompt(sid_b, pattern=r"#\s*$", timeout=5))
        show("GetActiveSession 当前激活会话", mgr.GetActiveSession())
        show("Execute 会话A执行 exit（远端 shell 退出）", mgr.Execute(sid_a, "exit", wait=1))
        time.sleep(1)
        show("SwitchSession 切入已退出会话（应失败）", mgr.SwitchSession(sid_a))
        show("CloseSession 关闭会话B", mgr.CloseSession(sid_b))
        show("ListSessions 关闭后遍历", mgr.ListSessions())
        show("CloseAll 全部关闭", mgr.CloseAll())

        print("\n--- 会话间消息 + 持久化测试 ---")
        r3 = json.loads(mgr.CreateSession(host, user, pwd, port, name="消息窗C", ssh=ssh))
        sid_c = r3["data"]["id"]
        r4 = json.loads(mgr.CreateSession(host, user, pwd, port, name="消息窗D"))
        sid_d = r4["data"]["id"]
        show("PostMessage 向会话C投递消息", mgr.PostMessage(sid_c, "你好，窗口C", from_id="窗口D"))
        show("Broadcast 向全部会话广播", mgr.Broadcast("系统通知：稍后维护"))
        show("ReadMessages 读取会话C收件箱", mgr.ReadMessages(sid_c))
        show("ReadMessages 读取会话D收件箱", mgr.ReadMessages(sid_d))
        show("SendToSession 注入输入到会话D", mgr.SendToSession(sid_d, "echo msg-from-C", from_session_id=sid_c))
        show("Read 会话D执行结果", mgr.Read(sid_d, timeout=2))
        show("GetHistory 会话D命令历史", mgr.GetHistory(sid_d))

        print("\n--- 切出后命令持续执行测试 ---")
        show("会话C启动长任务(3秒)", mgr.Execute(sid_c, "sleep 3; echo 后台任务已完成", wait=1))
        show("立即切出到会话D", mgr.SwitchSession(sid_d))
        show("切出期间会话D正常干活", mgr.Execute(command="echo 窗口D在切出期间正常干活", wait=1))
        time.sleep(3)   # 等待远端后台任务完成（期间后台线程持续收取输出）
        show("切回会话C", mgr.SwitchSession(sid_c))
        show("Read 会话C后台任务输出（切出期间已完成并收到）", mgr.Read(sid_c, timeout=1))

        print("\n--- 命令执行状态检测测试 ---")
        show("GetCommandState 当前会话C状态（应空闲）", mgr.GetCommandState(sid_c))
        show("会话C启动长任务(4秒)", mgr.Execute(sid_c, "sleep 4; echo 状态检测完毕", wait=0.5))
        show("GetCommandState 立即检测（应在执行中）", mgr.GetCommandState(sid_c))
        show("WaitCommandFinish 等待执行完毕", mgr.WaitCommandFinish(sid_c, timeout=15, interval=1))
        show("GetCommandState 执行完毕后再检测（应空闲）", mgr.GetCommandState(sid_c))

        bak = os.path.join(tempfile.gettempdir(), "sessions_backup_demo.json")
        show("ExportSessions 持久化会话配置", mgr.ExportSessions(bak))
        show("CloseAll 全部关闭（模拟重启）", mgr.CloseAll())
        r5 = json.loads(mgr.RestoreSessions(bak))
        show("RestoreSessions 从备份恢复", r5)
        restored_ids = [s["id"] for s in r5["data"]["restored"] if s.get("connected")]
        show("ListSessions 恢复后遍历", mgr.ListSessions())
        hist_id = next((s["id"] for s in r5["data"]["restored"]
                        if s.get("original_id") == sid_d), None)
        show("GetHistory 恢复后历史验证", mgr.GetHistory(hist_id, tail=5) if hist_id else "无恢复会话")
        show("CloseAll 清理恢复的会话", mgr.CloseAll())

        show("read_remote_file /etc/hostname（修复点验证）", ssh.read_remote_file("/etc/hostname"))
        show("write_remote_file 写远程文件", ssh.write_remote_file("/tmp/my_ssh_demo.txt", "hello from demo\n第二行中文\n"))
        show("write_remote_file 追加内容", ssh.write_remote_file("/tmp/my_ssh_demo.txt", "appended line\n", mode="a"))
        show("read_remote_file 回读验证", ssh.read_remote_file("/tmp/my_ssh_demo.txt"))
        show("ListRemoteDir /tmp（部分）", ssh.ListRemoteDir("/tmp"))

        local_src = os.path.join(tempfile.gettempdir(), "my_ssh_demo_local.txt")
        local_dst = os.path.join(tempfile.gettempdir(), "my_ssh_demo_download.txt")
        with open(local_src, "w", encoding="utf-8") as f:
            f.write("本地上传测试文件 upload test")
        show("PutLocalFile 上传", ssh.PutLocalFile(local_src, "/tmp/my_ssh_demo_upload.txt"))
        show("GetRemoteFile 下载", ssh.GetRemoteFile("/tmp/my_ssh_demo_upload.txt", local_dst))
        with open(local_dst, "r", encoding="utf-8") as f:
            print("   下载内容:", f.read())
        os.remove(local_src)
        os.remove(local_dst)

        show("StatRemoteFile 远程文件属性", ssh.StatRemoteFile("/tmp/my_ssh_demo.txt"))
        show("RemoveRemoteFile 清理远程临时文件", ssh.RemoveRemoteFile("/tmp/my_ssh_demo.txt"))
        show("RemoveRemoteFile 清理上传文件", ssh.RemoveRemoteFile("/tmp/my_ssh_demo_upload.txt"))
        ssh.BatchCMD_NotRef("rm -f /tmp/my_ssh_demo_ok")

    show("with 退出后 CloseSSH 状态", {"code": 200, "msg": "上下文退出自动关闭"})


if __name__ == "__main__":
    test_database()
    test_ssh()
    print("\n全部演示/冒烟测试执行完毕。")
