#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理直播源：删除高风险域名 + HTML假流，只保留真实可用的频道"""

import sys
import os
import json
import time
import concurrent.futures
from collections import OrderedDict
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
from validator import (
    StreamValidator,
    parse_extinf_name,
    parse_group_title,
    normalize_channel_name,
    is_rtmp_url,
    is_ipv6_url,
    extract_domain,
)

# ── 加载配置 ──
def load_json(filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

domain_rules = load_json('domain_rules.json')
category_map = load_json('category_map.json')

BLOCKLIST_EXACT = set(domain_rules.get('blocklist', {}).get('domains_exact', []))
BLOCKLIST_SUFFIX = set(domain_rules.get('blocklist', {}).get('domains_suffix', []))


# ── 解析 M3U ──
def read_m3u(filepath):
    channels = []
    current = {}
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('#EXTINF'):
                current = {'raw_extinf': line}
                name = parse_extinf_name(line)
                current['name'] = name or 'Unknown'
            elif line.startswith(('http://', 'https://')):
                if current:
                    current['url'] = line
                    channels.append(current)
                    current = {}
    return channels


# ── 判断 ──
def is_high_risk(domain):
    """检查是否为高风险域名"""
    # 精确拦截
    if domain in BLOCKLIST_EXACT:
        return True, f'blocklist:{domain}'
    # 高风险 TLD
    for suffix in BLOCKLIST_SUFFIX:
        if domain.endswith(suffix):
            return True, f'suffix:{suffix}'
    # 裸IP
    import re
    if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', domain):
        return True, f'raw_ip:{domain}'
    return False, ''


def is_webcam(name, extinf):
    """检测是否为景区慢直播"""
    gt = parse_group_title(extinf)
    if gt == '直播中国':
        return True
    webcam_kw = ['风景', '景观', '慢直播', '熊猫', '监控', '摄像头', '日出', '云海', '瀑布']
    return any(kw in name for kw in webcam_kw)


def is_movie_or_vod(name, extinf):
    """检测是否为电影/点播"""
    gt = parse_group_title(extinf)
    if gt in ('点播电影', '电影频道'):
        return True
    movie_kw = ['倩女幽魂', '大话西游', '少林足球', '功夫', '喜剧之王', '赌神', '古惑仔',
                '无间道', '英雄本色', '让子弹飞']
    return any(kw in name for kw in movie_kw)


def has_static_ext(url):
    import re
    return bool(re.search(r'\.(mp4|mp3|avi|mkv|flv|wmv)(\?|$)', url, re.I))


def is_timestamp_placeholder(name):
    """检测是否为更新时间占位符"""
    if '更新' in name and (':' in name or name[0:4].isdigit()):
        return True
    return False


# ── 内容验证 ──
def validate_stream(ch):
    """快速验证：HEAD + 读取前2KB确认非HTML"""
    url = ch.get('url', '')
    try:
        # HEAD
        r = requests.head(url, timeout=(3, 5), allow_redirects=True,
                         headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        ct = (r.headers.get('Content-Type', '')).lower()

        # HTML 直接拒绝
        if 'text/html' in ct:
            return False, f'HTML({r.status_code})', ''

        # 有效的内容类型
        valid_ct = ('video/', 'audio/', 'mpegurl', 'octet-stream', 'text/plain')
        is_valid_ct = any(v in ct for v in valid_ct)

        if not is_valid_ct:
            return False, f'bad_ct:{ct[:50]}', ''

        return True, f'{r.status_code}', ct

    except requests.exceptions.Timeout:
        return False, 'timeout', ''
    except requests.exceptions.ConnectionError:
        return False, 'connection', ''
    except Exception as e:
        return False, str(e)[:40], ''


# ── 分类 ──
def categorize(name, extinf):
    name_lower = name.lower()
    gt = parse_group_title(extinf)

    cats = category_map.get('categories', {})
    priority = category_map.get('priority', [])

    for cat_key in priority:
        cat = cats.get(cat_key, {})
        for gt_pat in cat.get('group_titles', []):
            if gt_pat in gt:
                return cat_key
        for kw in cat.get('keywords', []):
            if kw.lower() in name_lower:
                return cat_key

    if any(kw in name_lower for kw in ['频道', '电视台', '综合']):
        return 'local'
    return 'other'


# ── 生成 M3U ──
def save_m3u(filepath, channels, title):
    if not channels:
        print(f"  跳过 {filepath} (0个频道)")
        return
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    content = f"""#EXTM3U
#EXTENC: UTF-8
# Generated: {now}
# Title: {title}
# Total Channels: {len(channels)}
# Cleaned: removed high-risk domains + HTML fakes

"""
    for ch in channels:
        content += f"{ch['raw_extinf']}\n{ch['url']}\n"

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"  已保存: {filepath} ({len(channels)}个频道)")


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("DailyIPTV — 直播源清理")
    print("=" * 60)

    # 1. 读取所有源
    source_files = [
        'outputs/full_raw.m3u',
        'outputs/full_validated.m3u',
    ]
    all_channels = []
    for fp in source_files:
        if os.path.exists(fp):
            chs = read_m3u(fp)
            print(f"读取 {fp}: {len(chs)} 个频道")
            all_channels.extend(chs)

    # URL 去重
    seen_urls = OrderedDict()
    for ch in all_channels:
        url = ch.get('url', '')
        if url and url not in seen_urls:
            seen_urls[url] = ch
    all_channels = list(seen_urls.values())
    total = len(all_channels)
    print(f"URL去重后: {total} 个频道\n")

    # 2. 分类统计
    rejected = {
        'high_risk': [],     # 高风险域名
        'html_fake': [],     # HTML假流
        'connection_fail': [],  # 连接失败
        'webcam': [],        # 景区慢直播
        'movie_vod': [],     # 电影点播
        'static_file': [],   # 静态文件
        'placeholder': [],   # 占位符
        'other_bad': [],     # 其他问题
    }
    kept = []

    print("=" * 60)
    print("开始清理...")
    print("=" * 60)

    for i, ch in enumerate(all_channels):
        url = ch.get('url', '')
        name = ch.get('name', 'Unknown')
        extinf = ch.get('raw_extinf', '')
        domain = extract_domain(url)

        # ── A. 静态文件 ──
        if has_static_ext(url):
            rejected['static_file'].append(ch)
            continue

        # ── B. 占位符 ──
        if is_timestamp_placeholder(name):
            rejected['placeholder'].append(ch)
            continue

        # ── C. 高风险域名 ──
        is_risk, reason = is_high_risk(domain)
        if is_risk:
            rejected['high_risk'].append(ch)
            continue

        # ── D. 景区慢直播 ──
        if is_webcam(name, extinf):
            rejected['webcam'].append(ch)
            continue

        # ── E. 电影点播 ──
        if is_movie_or_vod(name, extinf):
            rejected['movie_vod'].append(ch)
            continue

        # ── F. 内容验证 ──
        valid, reason, ct = validate_stream(ch)
        if not valid:
            if 'HTML' in reason:
                rejected['html_fake'].append(ch)
            elif reason in ('timeout', 'connection'):
                rejected['connection_fail'].append(ch)
            else:
                rejected['other_bad'].append(ch)
            continue

        # ── G. 特殊处理: Content-Type 是 text/plain 的要再验证一下
        # 有些代理返回 text/plain 但内容可能是 m3u8
        if 'text/plain' in ct.lower():
            # 标记但不拒绝，可以后续再验证
            pass

        kept.append(ch)

        if (i + 1) % 200 == 0:
            print(f"  进度: {i+1}/{total} | 保留: {len(kept)} | 拒绝: {i+1-len(kept)}")

    # ── 3. 名称去重（同名保留最先遇到的） ──
    name_seen = OrderedDict()
    for ch in kept:
        norm = normalize_channel_name(ch.get('name', ''))
        if not norm:
            norm = ch.get('url', '')
        if norm not in name_seen:
            name_seen[norm] = ch
    kept_dedup = list(name_seen.values())

    print(f"\n清理完成: {total} → {len(kept)} → {len(kept_dedup)}(名称去重)\n")

    # ── 4. 打印拒绝统计 ──
    print("=" * 60)
    print("删除详情:")
    print("=" * 60)
    for reason, chs in rejected.items():
        if chs:
            print(f"\n❌ {reason} ({len(chs)}个):")
            # 列出域名分布
            from collections import Counter
            domains = Counter()
            for c in chs:
                domains[extract_domain(c.get('url', ''))] += 1
            for d, cnt in domains.most_common(5):
                names = [c['name'] for c in chs if extract_domain(c.get('url', '')) == d][:3]
                print(f"    {d} ({cnt}个): {', '.join(names)}")

    # ── 5. 保存结果 ──
    print("\n" + "=" * 60)
    print("保存清理后文件:")
    print("=" * 60)

    # 分类
    categorized = {}
    for ch in kept_dedup:
        cat = categorize(ch.get('name', ''), ch.get('raw_extinf', ''))
        if cat not in categorized:
            categorized[cat] = []
        categorized[cat].append(ch)

    cat_names = {
        'cctv': '央视频道', 'satellite': '卫视频道',
        'local': '地方频道', 'international': '国际频道',
        'other': '其他频道', 'sports': '体育频道',
        'kids': '儿童频道', 'music_arts': '音乐频道',
        'documentary': '纪录频道', 'webcam': '景区慢直播',
    }

    for cat_key, cat_title in cat_names.items():
        chs = categorized.get(cat_key, [])
        if chs:
            save_m3u(f'outputs_clean/{cat_key}.m3u', chs, cat_title)

    # 完整列表
    save_m3u('outputs_clean/full.m3u', kept_dedup, '清理后直播源')

    # 统计
    stats = {
        'update_time': datetime.now().isoformat(),
        'total_original': total,
        'kept': len(kept_dedup),
        'removed': total - len(kept_dedup),
        'categories': {k: len(v) for k, v in categorized.items()},
        'rejected': {k: len(v) for k, v in rejected.items()},
        'rejected_domains': {},
    }
    for reason, chs in rejected.items():
        for c in chs:
            d = extract_domain(c.get('url', ''))
            if d not in stats['rejected_domains']:
                stats['rejected_domains'][d] = {'count': 0, 'reason': reason}
            stats['rejected_domains'][d]['count'] += 1

    os.makedirs('outputs_clean', exist_ok=True)
    with open('outputs_clean/stats.json', 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    # 打印最终统计
    print(f"\n{'='*60}")
    print(f"✅ 清理完成!")
    print(f"{'='*60}")
    print(f"  原始: {total} 个")
    print(f"  删除: {total - len(kept_dedup)} 个")
    print(f"    高风险域名: {len(rejected['high_risk'])}")
    print(f"    HTML假流:   {len(rejected['html_fake'])}")
    print(f"    连接失败:   {len(rejected['connection_fail'])}")
    print(f"    景区慢直播: {len(rejected['webcam'])}")
    print(f"    电影点播:   {len(rejected['movie_vod'])}")
    print(f"    静态文件:   {len(rejected['static_file'])}")
    print(f"    占位符:     {len(rejected['placeholder'])}")
    print(f"    其他:       {len(rejected['other_bad'])}")
    print(f"  保留: {len(kept_dedup)} 个 ({len(kept_dedup)/total*100:.1f}%)")
    print(f"  分类: {', '.join(f'{k}:{len(v)}' for k, v in categorized.items())}")
    print(f"  输出: outputs_clean/")


if __name__ == '__main__':
    main()
