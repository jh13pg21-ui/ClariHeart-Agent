import argparse
import getpass

from app.core.database import session_scope
from app.core.security import hash_password
from app.models.entities import UserAccount


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli.users")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("username")
    create.add_argument("--display-name", default="")
    create.add_argument("--admin", action="store_true")

    set_password = commands.add_parser("set-password")
    set_password.add_argument("username")

    disable = commands.add_parser("disable")
    disable.add_argument("username")
    return parser


def _read_confirmed_password() -> str:
    password = getpass.getpass("密码: ")
    confirmation = getpass.getpass("再次输入密码: ")
    if not password or password != confirmation:
        raise ValueError("密码为空或两次输入不一致")
    return password


def run(args: argparse.Namespace) -> None:
    with session_scope() as db:
        if args.command == "create":
            if db.query(UserAccount).filter(UserAccount.username == args.username).first():
                raise ValueError("用户名已存在")
            user = UserAccount(
                username=args.username,
                display_name=args.display_name or args.username,
                password_hash=hash_password(_read_confirmed_password()),
                password_algorithm="argon2id",
                must_reset_password=False,
                disabled=False,
            )
            user.roles = {"ROLE_ADMIN"} if args.admin else {"ROLE_USER"}
            db.add(user)
        else:
            user = db.query(UserAccount).filter(UserAccount.username == args.username).first()
            if user is None:
                raise ValueError("用户不存在")
            if args.command == "set-password":
                user.password_hash = hash_password(_read_confirmed_password())
                user.password_algorithm = "argon2id"
                user.must_reset_password = False
                user.disabled = False
            elif args.command == "disable":
                user.disabled = True
        db.commit()


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
