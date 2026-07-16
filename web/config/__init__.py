#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IVD平台 - 配置模块"""

import os
from dotenv import load_dotenv

load_dotenv()

# 混合连接方案
from hybrid_connection import get_db_host, get_redis_url, get_go_parser_url


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'ivd-secret-key-2026')
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')
    SUPER_ADMIN_PASSWORD = os.getenv('SUPER_ADMIN_PASSWORD', 'super2026')
    DB_HOST = get_db_host()
    DB_PORT = int(os.getenv('DB_PORT', '5432'))
    DB_USER = os.getenv('DB_USER', 'ivd_user')
    DB_PASSWORD = os.getenv('DB_PASSWORD', 'ivd_pass')
    DB_NAME = os.getenv('DB_NAME', 'ivd_fault_db')
    REDIS_URL = get_redis_url()
    MAX_CONTENT_LENGTH = int(os.getenv('MAX_CONTENT_LENGTH', 200 * 1024 * 1024))
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    ANALYSIS_TTL_HOURS = int(os.getenv('ANALYSIS_TTL_HOURS', '2'))
    UPLOAD_DIR = os.getenv('UPLOAD_DIR', '/app/uploads')
    GO_PARSER_URL = get_go_parser_url()


# 常量定义
VALID_MSG_TYPES = {'QRY^Q02', 'DSR^Q03', 'ORU^R01', 'ACK^R01', 'ACK^Q03'}
INVALID_MSG_TYPES = {'QCK^Q02', 'DSP^Q031'}
VALID_SPECIMEN = {'Ser', 'Plasma', 'Urine', 'BALF', 'CSF', 'Automated', 'Serum', 'Whole Blood'}
VALID_GENDER = {'M', 'F', 'O', '0', ''}