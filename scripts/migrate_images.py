#!/usr/bin/env python3
"""
数据迁移脚本：将 software_bugs_{model} 表中的单图片数据迁移到 bug_images_{model} 表
使用方法：python migrate_images.py [--dry-run] [--clean-old]
选项：
  --dry-run    仅预览迁移数据，不实际执行
  --clean-old  迁移后清空原表的 image_data 和 image_mime 字段
"""

import sys
import os
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'postgres'),
    'port': int(os.environ.get('DB_PORT', 5432)),
    'database': os.environ.get('DB_NAME', 'ivd'),
    'user': os.environ.get('DB_USER', 'ivd'),
    'password': os.environ.get('DB_PASSWORD', 'ivd123')
}

def get_connection():
    return psycopg2.connect(**DB_CONFIG)

def migrate_images(dry_run=False, clean_old=False):
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT tablename FROM pg_tables 
            WHERE tablename LIKE 'software_bugs_%' 
            AND schemaname = 'public'
        """)
        bug_tables = [row['tablename'] for row in cur.fetchall()]
        
        if not bug_tables:
            print("未找到任何 software_bugs_* 表")
            return
        
        print(f"找到 {len(bug_tables)} 个 Bug 表：{', '.join(bug_tables)}\n")
        
        total_migrated = 0
        total_cleaned = 0
        
        for tbl in bug_tables:
            model_name = tbl.replace('software_bugs_', '')
            img_tbl = f"bug_images_{model_name}"
            
            cur.execute(f"""
                SELECT COUNT(*) as cnt FROM information_schema.tables 
                WHERE table_name = %s AND table_schema = 'public'
            """, (img_tbl,))
            
            if cur.fetchone()['cnt'] == 0:
                print(f"⚠️  表 {img_tbl} 不存在，正在创建...")
                if not dry_run:
                    cur.execute(f"""
                        CREATE TABLE {img_tbl} (
                            id SERIAL PRIMARY KEY,
                            bug_id INTEGER NOT NULL REFERENCES {tbl}(id) ON DELETE CASCADE,
                            image_data BYTEA NOT NULL,
                            image_mime TEXT NOT NULL,
                            sort_order INTEGER DEFAULT 0,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    cur.execute(f"CREATE INDEX idx_bugimg_{model_name}_bug ON {img_tbl}(bug_id)")
                    conn.commit()
                    print(f"   ✓ 已创建表 {img_tbl}")
                else:
                    print(f"   [dry-run] 将创建表 {img_tbl}")
            
            cur.execute(f"""
                SELECT id, image_data, image_mime 
                FROM {tbl} 
                WHERE image_data IS NOT NULL
            """)
            rows = cur.fetchall()
            
            if not rows:
                print(f"✓ {tbl}: 无图片数据")
                continue
            
            print(f"📸 {tbl}: 找到 {len(rows)} 条图片记录")
            
            if dry_run:
                for row in rows:
                    img_size = len(row['image_data']) if row['image_data'] else 0
                    print(f"   - Bug ID {row['id']}: {img_size/1024:.1f} KB ({row['image_mime']})")
                total_migrated += len(rows)
            else:
                for row in rows:
                    cur.execute(f"""
                        INSERT INTO {img_tbl} (bug_id, image_data, image_mime, sort_order)
                        VALUES (%s, %s, %s, 0)
                        ON CONFLICT DO NOTHING
                    """, (row['id'], row['image_data'], row['image_mime']))
                    total_migrated += 1
                
                if clean_old:
                    cur.execute(f"""
                        UPDATE {tbl} 
                        SET image_data = NULL, image_mime = NULL
                        WHERE image_data IS NOT NULL
                    """)
                    total_cleaned += cur.rowcount
                    print(f"   ✓ 已迁移 {len(rows)} 张图片，并清空原表图片字段")
                else:
                    print(f"   ✓ 已迁移 {len(rows)} 张图片")
                
                conn.commit()
        
        print(f"\n{'='*50}")
        if dry_run:
            print(f"预览完成：共 {total_migrated} 张图片待迁移")
        else:
            print(f"迁移完成：共迁移 {total_migrated} 张图片")
            if clean_old:
                print(f"已清空 {total_cleaned} 条原表图片记录")

if __name__ == '__main__':
    dry_run = '--dry-run' in sys.argv
    clean_old = '--clean-old' in sys.argv
    
    if '--help' in sys.argv or '-h' in sys.argv:
        print(__doc__)
        sys.exit(0)
    
    print("="*50)
    print("Bug 图片数据迁移脚本")
    print("="*50)
    if dry_run:
        print("模式：预览（dry-run）\n")
    else:
        print("模式：执行迁移")
        if clean_old:
            print("选项：迁移后清空原表图片字段\n")
        else:
            print("选项：保留原表图片字段\n")
    
    try:
        migrate_images(dry_run=dry_run, clean_old=clean_old)
    except Exception as e:
        print(f"\n❌ 错误：{e}")
        sys.exit(1)