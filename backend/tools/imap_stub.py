"""A stub IMAP4 server that serves messages, for proving the connected-mailbox route.

WHY THIS EXISTS. The route "organisation connects its own Gmail, a verification code arrives there, the
browser task proceeds" cannot be tested with a real mailbox in CI, and mocking the transport would skip
the parts that actually broke. IMAP is a text protocol, so answering the handful of commands imaplib
issues runs the REAL client, the REAL parser, the REAL ingest and the REAL verification engine.

IT FOUND TWO REAL DEFECTS:
  * gent_api stored the SSL flag as "ssl" while the transport builder read "use_ssl", so a mailbox
    connected with SSL off was silently built with SSL on and could never reach its server.
  * GranadaMail.sync never called VerificationService.examine - only the webhook ingest path did - so
    a code arriving at a CONNECTED mailbox was stored, classified, and never compared to the waiting
    request. The browser task would time out with the code sitting in the database.

TWO PROPERTIES THAT MAKE IT USEFUL RATHER THAN MISLEADING:
  * The message is dated NOW, not at a fixed date. A fixture dated in the past measures the engine's
    "this message arrived before the request was made" rule instead of the path under test.
  * It answers UID 2. The provider keys provider_message_id on the UID, so a stub that always answers
    UID 1 makes every re-sync dedupe as already seen and no genuinely new message can ever exist.

Run it inside the compose network and point a mailbox at pi:1143 with SSL off:

    docker compose cp backend/tools/imap_stub.py api:/tmp/imap_stub.py
    docker compose exec -d api python /tmp/imap_stub.py 1143

Then connect a mailbox with host pi, port 1143, use_ssl false, and sync it with
	ools/mail_sync_once.py.
"""
import socket
import sys
import threading

import email.utils as _email_utils
import time as _time

# DATED NOW, NOT A YEAR AGO. The engine refuses a message that arrived before the request was made -
# "it belongs to an earlier attempt" - and that rule was right when the first stub carried a stale
# Date header. A fixture dated in the past measures the safety rule instead of the path under test.
MESSAGE = (
    "From: UN Partner Portal <no-reply@unpartnerportal.org>\r\n"
    "To: ngo@example.org\r\n"
    "Subject: Your verification code\r\n"
    f"Date: {_email_utils.formatdate(_time.time())}\r\n"
    f"Message-ID: <stub-{int(_time.time())}@unpartnerportal.org>\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Your verification code is 481920. It expires in 15 minutes.\r\n"
)

CAPS = "IMAP4rev1 AUTH=PLAIN"


def handle(conn: socket.socket) -> None:
    try:
        conn.sendall(f"* OK [CAPABILITY {CAPS}] stub ready\r\n".encode())
        buf = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                if not line.strip():
                    continue
                text = line.decode("utf-8", "replace")
                tag, _, rest = text.partition(" ")
                verb = rest.upper()

                if verb.startswith("CAPABILITY"):
                    conn.sendall(f"* CAPABILITY {CAPS}\r\n{tag} OK CAPABILITY done\r\n".encode())
                elif verb.startswith("LOGIN"):
                    conn.sendall(f"{tag} OK LOGIN done\r\n".encode())
                elif verb.startswith("SELECT") or verb.startswith("EXAMINE"):
                    conn.sendall(
                        (
                            "* 1 EXISTS\r\n"
                            "* 0 RECENT\r\n"
                            "* OK [UIDVALIDITY 1] ok\r\n"
                            "* OK [UIDNEXT 2] ok\r\n"
                            f"{tag} OK [READ-ONLY] SELECT done\r\n"
                        ).encode()
                    )
                elif verb.startswith("UID SEARCH") or verb.startswith("SEARCH"):
                    # UID 2, NOT 1. The provider keys `provider_message_id` on the UID, so a stub that
                    # always answers UID 1 makes every re-sync dedupe as "already seen" - which is why
                    # a genuinely new message could not be produced in the previous round.
                    conn.sendall(f"* SEARCH 2\r\n{tag} OK SEARCH done\r\n".encode())
                elif verb.startswith("UID FETCH") or verb.startswith("FETCH"):
                    body = MESSAGE.encode()
                    conn.sendall(
                        (
                            f"* 2 FETCH (UID 2 RFC822 {{{len(body)}}}\r\n"
                        ).encode()
                        + body
                        + b")\r\n"
                        + f"{tag} OK FETCH done\r\n".encode()
                    )
                elif verb.startswith("LIST") or verb.startswith("LSUB"):
                    conn.sendall(f'* LIST (\\HasNoChildren) "/" "INBOX"\r\n{tag} OK LIST done\r\n'.encode())
                elif verb.startswith("NOOP") or verb.startswith("CHECK"):
                    conn.sendall(f"{tag} OK done\r\n".encode())
                elif verb.startswith("CLOSE"):
                    conn.sendall(f"{tag} OK CLOSE done\r\n".encode())
                elif verb.startswith("LOGOUT"):
                    conn.sendall(f"* BYE bye\r\n{tag} OK LOGOUT done\r\n".encode())
                    return
                else:
                    conn.sendall(f"{tag} NO unsupported: {verb}\r\n".encode())
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1143
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(8)
    print(f"stub IMAP listening on {port}", flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
