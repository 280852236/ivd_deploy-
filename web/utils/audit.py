import logging as _logging

_logger = _logging.getLogger(__name__)


def audit_log(action, target_type=None, target_id=None, detail=None, user_id=None, username=None, ip_address=None):
    try:
        from flask import session as _flask_session, request as _flask_request
        if user_id is None:
            user_id = _flask_session.get('user_id')
        if username is None:
            username = _flask_session.get('username')
        if ip_address is None:
            ip_address = _flask_request.remote_addr if _flask_request else None
    except Exception:
        pass
    try:
        from shared import db_connection
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO audit_logs (user_id, username, action, target_type, target_id, detail, ip_address) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (user_id, username, action, target_type, str(target_id) if target_id is not None else None, detail, ip_address)
            )
            conn.commit()
    except Exception as e:
        _logger.warning(f"审计日志写入失败: {e}")
