#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DailyIPTV — 每日IPTV直播源聚合更新脚本 (优化版)

改进:
  - Phase 1 HTTP HEAD + Phase 2 内容探测（验证m3u8/TS真实性）
  - 域名黑白名单过滤私人代理
  - A/B/C/F 四级质量评分
  - IPv6 源保留（ipv6.m3u）
  - 景区慢直播分离（webcam.m3u）
  - 频道名称去重
  - 过期 token 检测
"""

import requests
import json
import re
import os
import time
import concurrent.futures
from datetime import datetime, timezone
from urllib.parse import urlparse
from collections import OrderedDict

# 导入验证模块
from validator import (
    StreamValidator,
    is_ipv6_url,
    is_rtmp_url,
    extract_domain,
    parse_group_title,
    parse_extinf_name,
    normalize_channel_name,
    has_static_extension,
    has_token_params,
)


# ── 配置加载 ──────────────────────────────────────────────

def _load_json(filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


# ── IPTVUpdater ───────────────────────────────────────────

class IPTVUpdater:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/125.0.0.0 Safari/537.36'
            )
        })
        self.log_messages = []
        self.repository_owner = os.environ.get('GITHUB_REPOSITORY_OWNER', 'mymsnn')
        self.repository_name = os.environ.get('GITHUB_REPOSITORY', 'DailyIPTV').split('/')[-1]

        # 加载配置
        self.domain_rules = _load_json('domain_rules.json')
        self.category_map = _load_json('category_map.json')
        self.quality_tiers = _load_json('quality_tiers.json')

        # 初始化验证器
        self.validator = StreamValidator(
            timeout=5,
            content_probe_timeout=8,
            max_workers=10,
        )

    # ── 日志 ──────────────────────────────────────────

    def log(self, message):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        msg = f"[{timestamp}] {message}"
        print(msg)
        self.log_messages.append(msg)

    # ── 源加载 ────────────────────────────────────────

    def load_sources(self):
        try:
            with open('scripts/sources_list.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            self.log(f"加载源列表失败: {e}")
            return {
                "sources": [
                    "https://raw.githubusercontent.com/iptv-org/iptv/master/index.m3u",
                    "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/global.m3u"
                ],
                "backup_sources": [
                    "https://gitlab.com/iptv-org/iptv/-/raw/master/index.m3u"
                ]
            }

    def fetch_source(self, url, timeout=20):
        try:
            self.log(f"正在获取: {url}")
            response = self.session.get(url, timeout=timeout)
            response.encoding = 'utf-8'
            if response.status_code == 200:
                return response.text
            else:
                self.log(f"获取失败，状态码: {response.status_code}")
                return None
        except Exception as e:
            self.log(f"获取异常: {e}")
            return None

    def parse_m3u(self, content, source_url):
        channels = []
        current_channel = {}
        lines = content.splitlines()

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            if line.startswith('#EXTINF'):
                current_channel = {'raw_extinf': line}
                name = parse_extinf_name(line)
                if name:
                    current_channel['name'] = name
                else:
                    current_channel['name'] = f"Unknown_{i}"

            elif line.startswith(('http://', 'https://', 'rtsp://', 'rtmp://')):
                if current_channel:
                    current_channel['url'] = line
                    current_channel['source'] = source_url
                    channels.append(current_channel)
                    current_channel = {}

        self.log(f"从该源解析出 {len(channels)} 个频道")
        return channels

    # ── 频道分离 ──────────────────────────────────────

    def separate_channels(self, channels):
        """分离IPv4、IPv6、RTMP、拦截频道"""
        ipv4 = []
        ipv6 = []
        rtmp_rtsp = []
        blocked = []

        for ch in channels:
            url = ch.get('url', '')
            if is_ipv6_url(url):
                ipv6.append(ch)
                continue
            if is_rtmp_url(url):
                rtmp_rtsp.append(ch)
                continue

            # 域名拦截检查
            domain = extract_domain(url)
            blocklist_exact = set(
                self.domain_rules.get('blocklist', {}).get('domains_exact', [])
            )
            if domain in blocklist_exact:
                blocked.append(ch)
                continue

            ipv4.append(ch)

        return {
            'ipv4': ipv4,
            'ipv6': ipv6,
            'rtmp': rtmp_rtsp,
            'blocked': blocked,
        }

    # ── 去重 ──────────────────────────────────────────

    def dedup_by_url(self, channels):
        """按URL去重"""
        seen = OrderedDict()
        for ch in channels:
            url = ch.get('url', '')
            if url and url not in seen:
                seen[url] = ch
        return list(seen.values())

    def dedup_by_name(self, channels):
        """按频道名称去重，同名保留评分最高的源"""
        name_map = {}
        for ch in channels:
            norm = normalize_channel_name(ch.get('name', ''))
            if not norm:
                norm = ch.get('url', '')  # 无名频道用URL区分

            if norm not in name_map:
                name_map[norm] = ch
            else:
                # 保留评分更高的
                existing_score = name_map[norm].get('quality_score', 0)
                current_score = ch.get('quality_score', 0)
                if current_score > existing_score:
                    name_map[norm] = ch
                elif current_score == existing_score:
                    # 同分保留官方CDN的
                    existing_trusted = name_map[norm].get('domain_rules', {}).get('is_trusted', False)
                    current_trusted = ch.get('domain_rules', {}).get('is_trusted', False)
                    if current_trusted and not existing_trusted:
                        name_map[norm] = ch

        return list(name_map.values())

    # ── 验证 ──────────────────────────────────────────

    def validate_channels(self, channels):
        """并行验证频道，返回附带详细结果"""
        valid = []
        results = []
        total = len(channels)

        if total == 0:
            return valid, results

        self.log(f"开始验证 {total} 个频道 (Phase 1 HEAD + Phase 2 内容探测)...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.validator.max_workers) as executor:
            future_map = {
                executor.submit(self.validator.validate_channel, ch): ch
                for ch in channels
            }

            completed = 0
            for future in concurrent.futures.as_completed(future_map):
                ch = future_map[future]
                try:
                    result = future.result()
                    if result.get('valid'):
                        ch['quality_score'] = result.get('score', 0)
                        ch['tier'] = result.get('verdict', 'C')
                        ch['validation'] = result
                        ch['domain_rules'] = result.get('domain_rules', {})
                        valid.append(ch)
                    else:
                        ch['quality_score'] = 0
                        ch['tier'] = 'F'
                        ch['validation'] = result
                    results.append(result)

                except Exception as e:
                    results.append({
                        'channel': ch.get('name', 'Unknown'),
                        'url': ch.get('url', ''),
                        'valid': False,
                        'verdict': 'F',
                        'score': 0,
                        'error': str(e),
                    })

                completed += 1
                if completed % 50 == 0 or completed == total:
                    self.log(f"已验证 {completed}/{total} 个频道")

        return valid, results

    # ── 分类 ──────────────────────────────────────────

    def categorize_channel(self, channel_name, extinf_line=''):
        """根据频道名称和group-title分类"""
        name_lower = channel_name.lower()
        group_title = parse_group_title(extinf_line)
        group_lower = group_title.lower()

        categories = self.category_map.get('categories', {})
        priority = self.category_map.get('priority', [])

        # 按优先级匹配
        for cat_key in priority:
            cat = categories.get(cat_key, {})
            keywords = cat.get('keywords', [])
            group_titles = cat.get('group_titles', [])

            # 匹配 group-title
            for gt in group_titles:
                if gt in group_title:
                    return cat_key

            # 匹配关键词
            for kw in keywords:
                if kw.lower() in name_lower:
                    return cat_key

        # 兜底分类
        # 包含频道/台等关键词 -> local
        if any(kw in name_lower for kw in ['频道', '电视台', '广播', '综合']):
            return 'local'

        return 'other'

    def is_webcam_content(self, channel):
        """检测是否为景区慢直播"""
        name = channel.get('name', '')
        extinf = channel.get('raw_extinf', '')
        group = parse_group_title(extinf)

        if group == '直播中国':
            return True

        webcam_cat = self.category_map.get('categories', {}).get('webcam', {})
        for kw in webcam_cat.get('keywords', []):
            if kw in name:
                return True
        return False

    # ── M3U 生成 ──────────────────────────────────────

    def generate_m3u(self, channels, title="直播源"):
        """生成 M3U 内容"""
        now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        now_local = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        header = f"""#EXTM3U
#EXTENC: UTF-8
# Generated: {now_utc}
# Updated: {now_local}
# Title: {title}
# Total Channels: {len(channels)}
# For personal testing only.

"""
        content = header
        for ch in channels:
            extinf = ch.get('raw_extinf', f'#EXTINF:-1 ,{ch.get("name", "Unknown")}')
            url = ch.get('url', '')

            # 添加质量标注（可选）
            tier = ch.get('tier', '')
            score = ch.get('quality_score', '')
            if tier and score is not None:
                extinf_comment = f' # Tier:{tier} Score:{score}'
                if not extinf.rstrip().endswith(extinf_comment):
                    extinf = extinf.rstrip() + extinf_comment

            content += f"{extinf}\n{url}\n"

        return content

    def save_m3u(self, filepath, channels, title="直播源"):
        """保存 M3U 文件"""
        if not channels:
            self.log(f"跳过 {filepath} (0个频道)")
            return
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        content = self.generate_m3u(channels, title)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        self.log(f"已保存: {filepath} ({len(channels)}个频道)")

    # ── README 更新 ───────────────────────────────────

    def update_readme(self, stats):
        try:
            with open('README.md', 'r', encoding='utf-8') as f:
                readme_content = f.read()

            base_url = (
                f"https://raw.githubusercontent.com/"
                f"{self.repository_owner}/{self.repository_name}/main/outputs"
            )
            update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            tier_a_count = len(stats.get('category_channels', {}).get('tier_a', []))
            tier_b_count = len(stats.get('category_channels', {}).get('tier_b', []))
            tier_c_count = len(stats.get('category_channels', {}).get('tier_c', []))
            ipv6_count = len(stats.get('category_channels', {}).get('ipv6', []))
            blocked_count = len(stats.get('category_channels', {}).get('blocked', []))
            webcam_count = len(stats.get('category_channels', {}).get('webcam', []))

            section = f"""
## 📡 直播源地址

最后更新: {update_time}

### 🏆 质量分级
- **⭐ A级 (官方CDN)**: [{base_url}/tier_a.m3u]({base_url}/tier_a.m3u) ({tier_a_count}个)
- **✅ B级 (可靠聚合)**: [{base_url}/tier_b.m3u]({base_url}/tier_b.m3u) ({tier_b_count}个)
- **⚠️ C级 (低置信度)**: [{base_url}/tier_c.m3u]({base_url}/tier_c.m3u) ({tier_c_count}个)

### ✅ 综合验证列表
- **完整列表 (A+B+C)**: [{base_url}/full_validated.m3u]({base_url}/full_validated.m3u)
- 有效频道: {stats['valid_channels']} 个
- 有效率: {stats['validity_ratio']:.1%}

### 📺 分类频道
- **央视**: [{base_url}/cctv.m3u]({base_url}/cctv.m3u) ({stats['categories']['cctv']}个)
- **卫视**: [{base_url}/satellite.m3u]({base_url}/satellite.m3u) ({stats['categories']['satellite']}个)
- **地方台**: [{base_url}/local.m3u]({base_url}/local.m3u) ({stats['categories']['local']}个)
- **国际**: [{base_url}/international.m3u]({base_url}/international.m3u) ({stats['categories']['international']}个)
- **其他**: [{base_url}/other.m3u]({base_url}/other.m3u) ({stats['categories']['other']}个)

### 🔧 特殊列表
- **IPv6 源**: [{base_url}/ipv6.m3u]({base_url}/ipv6.m3u) ({ipv6_count}个，需IPv6网络)
- **景区慢直播**: [{base_url}/webcam.m3u]({base_url}/webcam.m3u) ({webcam_count}个)
- **已拦截**: [{base_url}/blocked.m3u]({base_url}/blocked.m3u) ({blocked_count}个，私人代理/高风险域名)

### 📊 统计信息
- 总采集: {stats['total_channels']} 个
- 内容验证通过: {stats.get('content_verified', 0)} 个
- IPv6保留: {ipv6_count} 个
- A级: {tier_a_count} | B级: {tier_b_count} | C级: {tier_c_count}
- 验证耗时: {stats['validation_seconds']} 秒
- 更新时间: {stats['update_time']}

---

"""

            if '## 📡 直播源地址' in readme_content:
                pattern = r'## 📡 直播源地址.*?---'
                readme_content = re.sub(
                    pattern, section.strip(), readme_content, flags=re.DOTALL
                )
            else:
                readme_content = readme_content.replace(
                    '# DailyIPTV 📺', f'# DailyIPTV 📺\n{section}'
                )

            with open('README.md', 'w', encoding='utf-8') as f:
                f.write(readme_content)

            self.log("README更新成功")
            return True
        except Exception as e:
            self.log(f"更新README失败: {e}")
            return False

    # ── 主流程 ────────────────────────────────────────

    def run(self):
        start_time = time.time()
        self.log("=" * 60)
        self.log("DailyIPTV 直播源聚合更新 (优化版)")
        self.log("=" * 60)

        # ── 1. 加载源配置 ──
        sources_config = self.load_sources()

        # ── 2. 采集所有源 ──
        all_channels = []
        successful_sources = 0

        for source_url in sources_config.get('sources', []):
            content = self.fetch_source(source_url)
            if content:
                channels = self.parse_m3u(content, source_url)
                all_channels.extend(channels)
                successful_sources += 1
                time.sleep(0.5)

        # 主源失败则尝试备用源
        if successful_sources == 0:
            self.log("⚠️ 所有主源失败，尝试备用源...")
            for backup_url in sources_config.get('backup_sources', []):
                content = self.fetch_source(backup_url)
                if content:
                    channels = self.parse_m3u(content, backup_url)
                    all_channels.extend(channels)
                    successful_sources += 1
                    time.sleep(0.5)

        if not all_channels:
            self.log("❌ 无法获取任何源，退出")
            return

        self.log(f"采集完成: {len(all_channels)} 个原始频道 (来自{successful_sources}个源)")

        # ── 3. URL去重 ──
        unique_channels = self.dedup_by_url(all_channels)
        self.log(f"URL去重后: {len(unique_channels)} 个频道")

        # ── 4. 保存原始列表 ──
        self.save_m3u('outputs/full_raw.m3u', unique_channels, "原始直播源（全量）")

        # ── 5. 频道分离 ──
        separated = self.separate_channels(unique_channels)
        ipv4_channels = separated['ipv4']
        ipv6_channels = separated['ipv6']
        rtmp_channels = separated['rtmp']
        blocked_channels = separated['blocked']

        self.log(
            f"分离: IPv4={len(ipv4_channels)} IPv6={len(ipv6_channels)} "
            f"RTMP={len(rtmp_channels)} 拦截={len(blocked_channels)}"
        )

        # ── 6. 保存特殊列表 ──
        self.save_m3u(
            'outputs/ipv6.m3u', ipv6_channels,
            "IPv6直播源（未验证，需IPv6网络）"
        )
        if rtmp_channels:
            self.save_m3u(
                'outputs/rtmp.m3u', rtmp_channels,
                "RTMP/RTSP直播源（HTTP无法验证）"
            )
        self.save_m3u(
            'outputs/blocked.m3u', blocked_channels,
            "已拦截直播源（私人代理/高风险域名）"
        )

        # ── 7. 验证 IPv4 频道 ──
        validation_start = time.time()
        valid_channels = []
        all_validation_results = []

        if ipv4_channels:
            valid_channels, all_validation_results = self.validate_channels(ipv4_channels)
        else:
            self.log("⚠️ 无IPv4频道可验证")

        validation_time = time.time() - validation_start

        # 内容验证统计
        content_verified = sum(
            1 for r in all_validation_results
            if r.get('phase2', {}).get('is_stream', False)
        )
        self.log(
            f"验证完成: HEAD通过={len(valid_channels)}, "
            f"内容验证={content_verified}/{len(ipv4_channels)}"
        )

        # ── 8. 分离景区慢直播 ──
        webcam_channels = [ch for ch in valid_channels if self.is_webcam_content(ch)]
        tv_channels = [ch for ch in valid_channels if ch not in set(webcam_channels)]

        self.log(f"景区慢直播: {len(webcam_channels)}个, 电视频道: {len(tv_channels)}个")
        self.save_m3u('outputs/webcam.m3u', webcam_channels, "景区慢直播")

        # ── 9. 名称去重（电视频道） ──
        deduped_tv = self.dedup_by_name(tv_channels)
        self.log(f"名称去重: {len(tv_channels)} → {len(deduped_tv)}个")

        # ── 10. 按质量分级 ──
        tier_a = [ch for ch in deduped_tv if ch.get('tier') == 'A']
        tier_b = [ch for ch in deduped_tv if ch.get('tier') == 'B']
        tier_c = [ch for ch in deduped_tv if ch.get('tier') == 'C']

        self.log(f"质量分级: A级={len(tier_a)} B级={len(tier_b)} C级={len(tier_c)}")

        self.save_m3u('outputs/tier_a.m3u', tier_a, "A级 — 官方CDN")
        self.save_m3u('outputs/tier_b.m3u', tier_b, "B级 — 可靠聚合源")
        self.save_m3u('outputs/tier_c.m3u', tier_c, "C级 — 低置信度")

        # ── 11. 综合验证列表 (A+B+C) ──
        all_validated = tier_a + tier_b + tier_c
        self.save_m3u('outputs/full_validated.m3u', all_validated, "已验证直播源（A+B+C级）")

        # ── 12. 分类 ──
        categorized = {
            'cctv': [], 'satellite': [], 'local': [],
            'international': [], 'other': [],
        }
        for ch in all_validated:
            cat = self.categorize_channel(
                ch.get('name', ''),
                ch.get('raw_extinf', '')
            )
            if cat in categorized:
                categorized[cat].append(ch)
            else:
                categorized['other'].append(ch)

        # 保存分类文件
        cat_names = {
            'cctv': '央视频道', 'satellite': '卫视频道',
            'local': '地方频道', 'international': '国际频道',
            'other': '其他频道',
        }
        for cat_key, cat_title in cat_names.items():
            ch_list = categorized[cat_key]
            self.save_m3u(f'outputs/{cat_key}.m3u', ch_list, cat_title)

        # ── 13. 保存验证详情 ──
        try:
            with open('logs/validation_details.json', 'w', encoding='utf-8') as f:
                json.dump(all_validation_results, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            self.log(f"保存验证详情失败: {e}")

        # ── 14. 保存评分详情 ──
        try:
            scored = []
            for ch in all_validated:
                scored.append({
                    'name': ch.get('name', ''),
                    'url': ch.get('url', ''),
                    'tier': ch.get('tier', ''),
                    'score': ch.get('quality_score', 0),
                    'domain': extract_domain(ch.get('url', '')),
                    'category': self.categorize_channel(
                        ch.get('name', ''), ch.get('raw_extinf', '')
                    ),
                })
            with open('outputs/scored_channels.json', 'w', encoding='utf-8') as f:
                json.dump(scored, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"保存评分详情失败: {e}")

        # ── 15. 日志 ──
        try:
            with open('logs/latest_update.log', 'w', encoding='utf-8') as f:
                f.write('\n'.join(self.log_messages))
        except Exception:
            pass

        # ── 16. 统计 ──
        end_time = time.time()
        duration = end_time - start_time
        total_valid = len(all_validated)
        quality_base = len(all_validation_results) if all_validation_results else 1

        # 错误分类统计
        error_counts = {}
        for r in all_validation_results:
            ec = r.get('phase1', {}).get('error_class', 'unknown')
            if ec != 'ok' or not r.get('valid'):
                error_counts[ec] = error_counts.get(ec, 0) + 1

        stats = {
            'update_time': datetime.now().isoformat(),
            'duration_seconds': round(duration, 2),
            'validation_seconds': round(validation_time, 2),
            'sources_attempted': (
                len(sources_config.get('sources', [])) +
                len(sources_config.get('backup_sources', []))
            ),
            'sources_successful': successful_sources,
            'total_channels': len(all_channels),
            'unique_channels': len(unique_channels),
            'ipv4_channels': len(ipv4_channels),
            'ipv6_channels': len(ipv6_channels),
            'rtmp_channels': len(rtmp_channels),
            'blocked_channels': len(blocked_channels),
            'webcam_channels': len(webcam_channels),
            'valid_channels': total_valid,
            'content_verified': content_verified,
            'validity_ratio': total_valid / max(len(ipv4_channels), 1),
            'tier_a': len(tier_a),
            'tier_b': len(tier_b),
            'tier_c': len(tier_c),
            'error_classification': error_counts,
            'categories': {k: len(v) for k, v in categorized.items()},
            'category_channels': {
                'tier_a': [ch.get('name', '') for ch in tier_a],
                'tier_b': [ch.get('name', '') for ch in tier_b],
                'tier_c': [ch.get('name', '') for ch in tier_c],
                'ipv6': [ch.get('name', '') for ch in ipv6_channels],
                'blocked': [ch.get('name', '') for ch in blocked_channels],
                'webcam': [ch.get('name', '') for ch in webcam_channels],
            },
        }

        with open('outputs/stats.json', 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

        # ── 17. 更新 README ──
        self.update_readme(stats)

        self.log("=" * 60)
        self.log(f"✅ 更新完成! 耗时: {duration:.1f}秒")
        self.log(f"   总采集: {len(unique_channels)} | 验证通过: {total_valid}")
        self.log(f"   A级: {len(tier_a)} | B级: {len(tier_b)} | C级: {len(tier_c)}")
        self.log(f"   IPv6保留: {len(ipv6_channels)} | 拦截: {len(blocked_channels)}")
        self.log(f"   内容验证: {content_verified} | 景区: {len(webcam_channels)}")
        self.log("=" * 60)


# ── 入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    updater = IPTVUpdater()
    updater.run()
