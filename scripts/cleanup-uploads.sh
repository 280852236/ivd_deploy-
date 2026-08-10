#!/bin/sh
UPLOAD_DIR="${UPLOAD_DIR:-/app/uploads}"
MAX_AGE_DAYS="${UPLOAD_CLEANUP_DAYS:-7}"
find "$UPLOAD_DIR" -type f -mtime +"$MAX_AGE_DAYS" -name "ivd_upload_*" -exec rm -rf {} + 2>/dev/null
find "$UPLOAD_DIR" -type d -empty -mtime +"$MAX_AGE_DAYS" -name "ivd_upload_*" -exec rmdir {} + 2>/dev/null
echo "$(date '+%Y-%m-%d %H:%M:%S') upload cleanup done"
