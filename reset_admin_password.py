#!/usr/bin/env python3
# reset_admin_password.py — 重置后台管理密码（在沙盒终端里手动执行）
#
# 库里存的是 pbkdf2_sha256 哈希，明文取不回来 —— 所以没有「查看当前密码」这回事，
# 忘了只能重置。默认生成随机强密码，也可以自己指定：
#
#   /opt/venv/bin/python3 /home/user/workspace/app/reset_admin_password.py
#   /opt/venv/bin/python3 /home/user/workspace/app/reset_admin_password.py 我的新密码
#
# 必须用 /opt/venv/bin/python3：passlib 只装在那个 venv 里。
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from passlib.context import CryptContext

WORKSPACE = Path("/home/user/workspace")
SOCKET = WORKSPACE / "data" / "mysql.sock"
PW_FILE = WORKSPACE / "ADMIN_PASSWORD.txt"
# 算法与 common/utils/security.py 保持一致，否则登录校验不通过
PWD_CTX = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def run_sql(sql: str, raw: bool = False) -> str:
    """SQL 走临时文件重定向，绝不拼进 shell 字符串。

    passlib 的哈希形如 $pbkdf2-sha256$29000$salt$hash，满是 $；拼进 shell 会被 bash
    当变量展开，写进库的是乱码，而 UPDATE 依旧返回成功、毫无异常 —— provision.py
    踩过这个坑，这里照样躲开。
    """
    with tempfile.NamedTemporaryFile("w", suffix=".sql", encoding="utf-8", delete=False) as tmp:
        tmp.write(sql)
        path = tmp.name
    try:
        with open(path, encoding="utf-8") as stdin:
            cmd = ["mariadb", f"--socket={SOCKET}", "-u", "root"]
            if raw:
                cmd += ["-N", "-B"]
            cmd.append("xianyu_data")
            p = subprocess.run(cmd, stdin=stdin, capture_output=True, text=True)
        if p.returncode != 0:
            sys.exit(f"数据库操作失败（exit {p.returncode}）：\n{p.stderr or p.stdout}")
        return p.stdout
    finally:
        Path(path).unlink(missing_ok=True)


def sql_quote(v: str) -> str:
    """SQL 字符串字面量转义（反斜杠与单引号）。只用于值，不用于标识符。"""
    return v.replace("\\", "\\\\").replace("'", "''")


def main() -> None:
    if len(sys.argv) > 2:
        sys.exit("用法：reset_admin_password.py [新密码]（密码含空格请加引号）")

    if len(sys.argv) == 2:
        password = sys.argv[1]
        if len(password) < 8:
            sys.exit("密码太短，至少 8 位")
        generated = False
    else:
        password = secrets.token_urlsafe(15)
        generated = True

    pwd_hash = PWD_CTX.hash(password)
    run_sql(f"UPDATE xy_users SET password_hash='{sql_quote(pwd_hash)}' WHERE username='admin';")

    # 读回校验：确认库里的哈希真能验证这个密码。UPDATE 成功不代表写对了
    # （见 run_sql 的注释），这一步才是唯一能发现哈希被写坏的地方。
    stored = run_sql(
        "SELECT password_hash FROM xy_users WHERE username='admin';", raw=True
    ).strip()
    if not stored:
        sys.exit("!! xy_users 里没有 admin 这个账号，数据库可能未初始化完成")
    if not PWD_CTX.verify(password, stored):
        sys.exit("!! 密码写入后校验失败，库里的哈希不是刚设置的这个，请检查数据库")

    # 同步落盘，免得项目页「文件」里那份 ADMIN_PASSWORD.txt 变成过期的误导信息
    note = "（本次由 reset_admin_password.py 重置）"
    try:
        PW_FILE.write_text(
            f"闲鱼卖家 后台登录\n用户名: admin\n密码: {password}\n\n{note}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"（提示：写入 {PW_FILE} 失败：{exc}）")

    print()
    print("=" * 46)
    print("  密码已重置" + ("（随机生成）" if generated else ""))
    print(f"  密码: {password}")
    print("=" * 46)
    print(f"已同步写入 {PW_FILE}")


if __name__ == "__main__":
    main()
