#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import json
import re
from datetime import datetime
import time
import os
import concurrent.futures
from urllib.parse import urlparse

class IPTVUpdater:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        self.all_channels = []
        self.log_messages = []
        self.repository_owner = os.environ.get('GITHUB_REPOSITORY_OWNER', 'mymsnn')
        self.repository_name = os.environ.get('GITHUB_REPOSITORY', 'DailyIPTV').split('/')[-1]
        
    def log(self, message):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_message = f"[{timestamp}] {message}"
        print(log_message)
        self.log_messages.append(log_message)
        
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
    
    def fetch_source(self, url, timeout=15):
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
                name_match = re.search(r',(?P<name>.*)$', line)
                if name_match:
                    current_channel['name'] = name_match.group('name').strip()
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
    
    def is_url_accessible(self, channel, timeout=3):
        url = channel['url']
        try:
            parsed_url = urlparse(url)
            if not parsed_url.scheme or not parsed_url.netloc:
                return False, "无效的URL格式"
            
            if any(domain in url for domain in ['youtube.com', 'youtu.be', 'twitch.tv']):
                return True, "流媒体链接（跳过验证）"
            
            response = requests.head(url, timeout=timeout, allow_redirects=True, headers={
                'User-Agent': 'Mozilla/5.0'
            })
            
            if response.status_code in [200, 302, 301]:
                return True, f"状态码: {response.status_code}"
            else:
                return False, f"状态码: {response.status_code}"
                
        except requests.exceptions.Timeout:
            return False, "连接超时"
        except requests.exceptions.ConnectionError:
            return False, "连接错误"
        except Exception as e:
            return False, f"异常: {str(e)}"
    
    def validate_channels_parallel(self, channels, max_workers=5):
        valid_channels = []
        validation_results = []
        
        self.log(f"开始并行验证 {len(channels)} 个频道...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_channel = {
                executor.submit(self.is_url_accessible, channel): channel 
                for channel in channels
            }
            
            for i, future in enumerate(concurrent.futures.as_completed(future_to_channel)):
                channel = future_to_channel[future]
                try:
                    is_valid, message = future.result()
                    if is_valid:
                        valid_channels.append(channel)
                    validation_results.append({
                        'channel': channel['name'],
                        'url': channel['url'],
                        'valid': is_valid,
                        'message': message
                    })
                    
                    if (i + 1) % 20 == 0:
                        self.log(f"已验证 {i + 1}/{len(channels)} 个频道")
                        
                except Exception as e:
                    validation_results.append({
                        'channel': channel['name'],
                        'url': channel['url'],
                        'valid': False,
                        'message': f"验证异常: {str(e)}"
                    })
        
        try:
            with open('logs/validation_details.json', 'w', encoding='utf-8') as f:
                json.dump(validation_results, f, ensure_ascii=False, indent=2)
        except:
            pass
        
        return valid_channels, validation_results
    
    def filter_quality_channels(self, channels):
        quality_channels = []
        
        for channel in channels:
            url = channel['url']
            name = channel['name'].lower()
            
            if any(bad_name in name for bad_name in ['test', 'example', 'demo', '无效', '测试']):
                continue
                
            if any(good_keyword in name for good_keyword in [
                'cctv', '央视', '卫视', '湖南', '浙江', '江苏', '北京', '上海', '广东',
                'bbc', 'cnn', 'disney', 'discovery'
            ]):
                quality_channels.append(channel)
                continue
                
            quality_channels.append(channel)
        
        return quality_channels
    
    def categorize_channel(self, channel_name):
        name_lower = channel_name.lower()
        
        cctv_keywords = ['cctv', '央视', '中央']
        if any(keyword in name_lower for keyword in cctv_keywords):
            return 'cctv'
        
        satellite_keywords = ['卫视', 'tvb', '凤凰', '湖南', '浙江', '江苏', '北京']
        if any(keyword in name_lower for keyword in satellite_keywords):
            return 'satellite'
        
        local_keywords = ['都市', '新闻', '民生', '公共', '教育', '少儿', '体育']
        if any(keyword in name_lower for keyword in local_keywords):
            return 'local'
        
        international_keywords = ['bbc', 'cnn', 'nhk', 'fox', 'hbo', 'disney']
        if any(keyword in name_lower for keyword in international_keywords):
            return 'international'
        
        return 'other'
    
    def generate_m3u_content(self, channels, title="直播源"):
        header = f"""#EXTM3U
#EXTENC: UTF-8
# Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
# Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Title: {title}
# Total Channels: {len(channels)}
# For personal testing only.

"""
        content = header
        for channel in channels:
            content += f"{channel['raw_extinf']}\n{channel['url']}\n"
        
        return content
    
    def update_readme(self, stats):
        try:
            with open('README.md', 'r', encoding='utf-8') as f:
                readme_content = f.read()
            
            base_url = f"https://raw.githubusercontent.com/{self.repository_owner}/{self.repository_name}/main/outputs"
            update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            live_sources_section = f"""
## 📡 直播源地址

最后更新: {update_time}

### ✅ 已验证列表
- **完整列表**: [{base_url}/full_validated.m3u]({base_url}/full_validated.m3u)
- 有效频道: {stats['valid_channels']} 个
- 有效性: {stats['validity_ratio']:.1%}

### 📺 分类频道
- **央视**: [{base_url}/cctv.m3u]({base_url}/cctv.m3u) ({stats['categories']['cctv']}个)
- **卫视**: [{base_url}/satellite.m3u]({base_url}/satellite.m3u) ({stats['categories']['satellite']}个)
- **地方台**: [{base_url}/local.m3u]({base_url}/local.m3u) ({stats['categories']['local']}个)
- **国际**: [{base_url}/international.m3u]({base_url}/international.m3u) ({stats['categories']['international']}个)

### 📊 统计信息
- 总频道: {stats['total_channels']} 个
- 验证耗时: {stats['validation_seconds']} 秒
- 更新时间: {stats['update_time']}

---

"""
            
            if '## 📡 直播源地址' in readme_content:
                pattern = r'## 📡 直播源地址.*?---'
                updated_readme = re.sub(pattern, live_sources_section.strip(), readme_content, flags=re.DOTALL)
            else:
                updated_readme = readme_content.replace('# DailyIPTV 📺', f'# DailyIPTV 📺\n{live_sources_section}')
            
            with open('README.md', 'w', encoding='utf-8') as f:
                f.write(updated_readme)
            
            self.log("README更新成功")
            return True
            
        except Exception as e:
            self.log(f"更新README失败: {e}")
            return False

    def run(self):
        start_time = time.time()
        self.log("开始更新IPTV直播源")
        
        self.sources_config = self.load_sources()
        
        all_channels = []
        successful_sources = 0
        
        for source_url in self.sources_config['sources']:
            content = self.fetch_source(source_url)
            if content:
                channels = self.parse_m3u(content, source_url)
                all_channels.extend(channels)
                successful_sources += 1
                time.sleep(1)
        
        if successful_sources == 0 and self.sources_config['backup_sources']:
            self.log("尝试备用源...")
            for backup_url in self.sources_config['backup_sources']:
                content = self.fetch_source(backup_url)
                if content:
                    channels = self.parse_m3u(content, backup_url)
                    all_channels.extend(channels)
                    successful_sources += 1
                    time.sleep(1)
        
        unique_channels = {}
        for channel in all_channels:
            url = channel['url']
            if url not in unique_channels:
                unique_channels[url] = channel
        
        unique_channels_list = list(unique_channels.values())
        self.log(f"去重后频道: {len(unique_channels_list)}")
        
        raw_content = self.generate_m3u_content(unique_channels_list, "原始直播源")
        with open('outputs/full_raw.m3u', 'w', encoding='utf-8') as f:
            f.write(raw_content)
        
        quality_channels = self.filter_quality_channels(unique_channels_list)
        self.log(f"质量过滤后: {len(quality_channels)}")
        
        validation_start = time.time()
        if quality_channels:
            valid_channels, validation_results = self.validate_channels_parallel(quality_channels)
        else:
            valid_channels = []
            validation_results = []
        validation_time = time.time() - validation_start
        
        self.log(f"验证完成: {len(valid_channels)}/{len(quality_channels)}")
        
        categorized_channels = {'cctv': [], 'satellite': [], 'local': [], 'international': [], 'other': []}
        for channel in valid_channels:
            category = self.categorize_channel(channel['name'])
            categorized_channels[category].append(channel)
        
        validated_content = self.generate_m3u_content(valid_channels, "已验证直播源")
        with open('outputs/full_validated.m3u', 'w', encoding='utf-8') as f:
            f.write(validated_content)
        
        for category, channels in categorized_channels.items():
            if channels:
                category_content = self.generate_m3u_content(channels, f"{category}频道")
                with open(f'outputs/{category}.m3u', 'w', encoding='utf-8') as f:
                    f.write(category_content)
        
        with open('logs/latest_update.log', 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.log_messages))
        
        end_time = time.time()
        duration = end_time - start_time
        validity_ratio = len(valid_channels) / len(quality_channels) if quality_channels else 0
        
        stats = {
            'update_time': datetime.now().isoformat(),
            'duration_seconds': round(duration, 2),
            'validation_seconds': round(validation_time, 2),
            'sources_attempted': len(self.sources_config['sources']) + len(self.sources_config['backup_sources']),
            'sources_successful': successful_sources,
            'total_channels': len(unique_channels_list),
            'quality_channels': len(quality_channels),
            'valid_channels': len(valid_channels),
            'validity_ratio': validity_ratio,
            'categories': {k: len(v) for k, v in categorized_channels.items()}
        }
        
        with open('outputs/stats.json', 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        
        self.update_readme(stats)
        
        self.log(f"更新完成！耗时: {duration:.1f}秒")
        self.log(f"有效频道: {len(valid_channels)}/{len(quality_channels)} ({validity_ratio:.1%})")

if __name__ == "__main__":
    updater = IPTVUpdater()
    updater.run()
