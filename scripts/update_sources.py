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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
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
        """加载源列表"""
        try:
            with open('scripts/sources_list.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            self.log(f"加载源列表失败: {e}")
            # 返回默认的源列表
            return {
                "sources": [
                    "https://raw.githubusercontent.com/iptv-org/iptv/master/index.m3u",
                    "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/global.m3u"
                ],
                "backup_sources": [
                    "https://gitlab.com/iptv-org/iptv/-/raw/master/index.m3u",
                    "https://raw.githubusercontent.com/EvilCult/iptv-m3u-maker/master/m3u/index.m3u"
                ]
            }
    
    def fetch_source(self, url, timeout=15):
        """获取单个源"""
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
        """解析M3U内容"""
        channels = []
        current_channel = {}
        lines = content.splitlines()
        
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
                
            if line.startswith('#EXTINF'):
                # 解析频道信息
                current_channel = {'raw_extinf': line}
                # 提取频道名称
                name_match = re.search(r',(?P<name>.*)$', line)
                if name_match:
                    current_channel['name'] = name_match.group('name').strip()
                else:
                    current_channel['name'] = f"Unknown_{i}"
                    
            elif line.startswith('http://') or line.startswith('https://') or line.startswith('rtsp://') or line.startswith('rtmp://'):
                if current_channel:
                    current_channel['url'] = line
                    current_channel['source'] = source_url
                    channels.append(current_channel)
                    current_channel = {}
        
        self.log(f"从该源解析出 {len(channels)} 个频道")
        return channels
    
    def is_url_accessible(self, channel, timeout=5):
        """检查URL是否可访问 - 改进版本"""
        url = channel['url']
        try:
            # 先检查URL格式是否有效
            parsed_url = urlparse(url)
            if not parsed_url.scheme or not parsed_url.netloc:
                return False, "无效的URL格式"
            
            # 对于某些类型的URL，使用更快的检查方式
            if 'youtube.com' in url or 'youtu.be' in url:
                return True, "YouTube链接（跳过验证）"
            
            if 'twitch.tv' in url:
                return True, "Twitch链接（跳过验证）"
            
            # 发送HEAD请求检查
            response = requests.head(url, timeout=timeout, allow_redirects=True, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            
            if response.status_code in [200, 302, 301]:
                return True, f"状态码: {response.status_code}"
            else:
                return False, f"状态码: {response.status_code}"
                
        except requests.exceptions.Timeout:
            return False, "连接超时"
        except requests.exceptions.ConnectionError:
            return False, "连接错误"
        except requests.exceptions.RequestException as e:
            return False, f"请求异常: {str(e)}"
        except Exception as e:
            return False, f"未知错误: {str(e)}"
    
    def validate_channels_parallel(self, channels, max_workers=10):
        """并行验证频道"""
        valid_channels = []
        validation_results = []
        
        self.log(f"开始并行验证 {len(channels)} 个频道...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有验证任务
            future_to_channel = {
                executor.submit(self.is_url_accessible, channel): channel 
                for channel in channels
            }
            
            # 处理完成的任务
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
                    
                    if (i + 1) % 50 == 0:
                        self.log(f"已验证 {i + 1}/{len(channels)} 个频道")
                        
                except Exception as e:
                    validation_results.append({
                        'channel': channel['name'],
                        'url': channel['url'],
                        'valid': False,
                        'message': f"验证异常: {str(e)}"
                    })
        
        # 保存详细的验证结果
        with open('logs/validation_details.json', 'w', encoding='utf-8') as f:
            json.dump(validation_results, f, ensure_ascii=False, indent=2)
        
        return valid_channels, validation_results
    
    def filter_quality_channels(self, channels):
        """过滤高质量频道"""
        quality_channels = []
        
        for channel in channels:
            url = channel['url']
            name = channel['name'].lower()
            
            # 过滤掉明显的低质量源
            if any(bad_keyword in url for bad_keyword in ['/hls/', '/live/', 'm3u8?']):
                # 这些可能是需要特殊处理的流，暂时保留
                pass
            
            # 过滤掉名称中包含明显无效信息的频道
            if any(bad_name in name for bad_name in ['test', 'example', 'demo', '无效', '测试']):
                continue
                
            # 保留知名电视台和高质量源
            if any(good_keyword in name for good_keyword in [
                'cctv', '央视', '卫视', '湖南', '浙江', '江苏', '北京', '上海', '广东',
                'bbc', 'cnn', 'disney', 'discovery', 'national geographic'
            ]):
                quality_channels.append(channel)
                continue
                
            # 其他频道也保留，但标记为需要验证
            quality_channels.append(channel)
        
        return quality_channels
    
    def categorize_channel(self, channel_name):
        """分类频道"""
        name_lower = channel_name.lower()
        
        # 央视分类
        cctv_keywords = ['cctv', '央视', '中央']
        if any(keyword in name_lower for keyword in cctv_keywords):
            return 'cctv'
        
        # 卫视分类
        satellite_keywords = ['卫视', 'tvb', '凤凰', '星空', '湖南', '浙江', '江苏', '东方', '北京']
        if any(keyword in name_lower for keyword in satellite_keywords):
            return 'satellite'
        
        # 地方台分类
        local_keywords = ['都市', '新闻', '民生', '公共', '教育', '少儿', '体育', '影视', '综艺']
        if any(keyword in name_lower for keyword in local_keywords):
            return 'local'
        
        # 国际频道
        international_keywords = ['bbc', 'cnn', 'nhk', 'fox', 'hbo', 'disney', 'discovery', '国家地理']
        if any(keyword in name_lower for keyword in international_keywords):
            return 'international'
        
        return 'other'
    
    def generate_m3u_content(self, channels, title="直播源"):
        """生成M3U内容"""
        header = f"""#EXTM3U
#EXTENC: UTF-8
# Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
# Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Title: {title}
# Total Channels: {len(channels)}
# For personal testing and research purposes only.

"""
        content = header
        for channel in channels:
            content += f"{channel['raw_extinf']}\n{channel['url']}\n"
        
        return content
    
    def update_readme(self, stats):
        """更新README文件"""
        try:
            with open('README.md', 'r', encoding='utf-8') as f:
                readme_content = f.read()
            
            base_url = f"https://raw.githubusercontent.com/{self.repository_owner}/{self.repository_name}/main/outputs"
            update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            live_sources_section = f"""
## 📡 直播源地址

以下是最新的直播源地址（最后更新: {update_time}）：

### 🌐 完整列表（已验证）
- **M3U格式**: [{base_url}/full_validated.m3u]({base_url}/full_validated.m3u)
- 有效频道: {stats['valid_channels']} 个
- 有效性比例: {stats['validity_ratio']:.2%}

### 🌐 原始列表（未验证）
- **M3U格式**: [{base_url}/full_raw.m3u]({base_url}/full_raw.m3u)
- 总频道: {stats['total_channels']} 个

### 📺 分类列表
- **央视频道**: [{base_url}/cctv.m3u]({base_url}/cctv.m3u) ({stats['categories']['cctv']} 个频道)
- **卫视频道**: [{base_url}/satellite.m3u]({base_url}/satellite.m3u) ({stats['categories']['satellite']} 个频道)
- **地方台**: [{base_url}/local.m3u]({base_url}/local.m3u) ({stats['categories']['local']} 个频道)
- **国际频道**: [{base_url}/international.m3u]({base_url}/international.m3u) ({stats['categories']['international']} 个频道)

### 📊 统计信息
- **更新耗时**: {stats['duration_seconds']} 秒
- **验证耗时**: {stats['validation_seconds']} 秒
- **成功源数**: {stats['sources_successful']} 个
- **更新时间**: {stats['update_time']}

### 🚀 推荐使用
建议使用 **已验证列表** 获得更好的观看体验！

---

"""
            
            if '## 📡 直播源地址' in readme_content:
                pattern = r'## 📡 直播源地址.*?---'
                updated_readme = re.sub(pattern, live_sources_section.strip(), readme_content, flags=re.DOTALL)
            else:
                updated_readme = readme_content.replace('# DailyIPTV 📺', f'# DailyIPTV 📺\n{live_sources_section}')
            
            with open('README.md', 'w', encoding='utf-8') as f:
                f.write(updated_readme)
            
            self.log("README文件更新成功！")
            return True
            
        except Exception as e:
            self.log(f"更新README文件失败: {e}")
            return False

    def run(self):
        """主运行函数"""
        start_time = time.time()
        self.log("=== 开始更新IPTV直播源 ===")
        
        # 加载配置
        self.sources_config = self.load_sources()
        
        # 获取所有源
        all_channels = []
        successful_sources = 0
        
        for source_url in self.sources_config['sources']:
            content = self.fetch_source(source_url)
            if content:
                channels = self.parse_m3u(content, source_url)
                all_channels.extend(channels)
                successful_sources += 1
                time.sleep(1)
        
        # 如果主要源失败，尝试备用源
        if successful_sources == 0 and self.sources_config['backup_sources']:
            self.log("主要源全部失败，尝试备用源...")
            for backup_url in self.sources_config['backup_sources']:
                content = self.fetch_source(backup_url)
                if content:
                    channels = self.parse_m3u(content, backup_url)
                    all_channels.extend(channels)
                    successful_sources += 1
                    time.sleep(1)
        
        # 去重：基于URL去重
        unique_channels = {}
        for channel in all_channels:
            url = channel['url']
            if url not in unique_channels:
                unique_channels[url] = channel
        
        unique_channels_list = list(unique_channels.values())
        self.log(f"去重后频道数量: {len(unique_channels_list)}")
        
        # 保存原始列表（未验证）
        raw_content = self.generate_m3u_content(unique_channels_list, "原始直播源（未验证）")
        with open('outputs/full_raw.m3u', 'w', encoding='utf-8') as f:
            f.write(raw_content)
        
        # 过滤高质量频道
        quality_channels = self.filter_quality_channels(unique_channels_list)
        self.log(f"质量过滤后频道数量: {len(quality_channels)}")
        
        # 并行验证频道
        validation_start = time.time()
        valid_channels, validation_results = self.validate_channels_parallel(quality_channels)
        validation_time = time.time() - validation_start
        
        self.log(f"验证完成！有效频道: {len(valid_channels)}/{len(quality_channels)}")
        
        # 分类频道
        categorized_channels = {'cctv': [], 'satellite': [], 'local': [], 'international': [], 'other': []}
        for channel in valid_channels:
            category = self.categorize_channel(channel['name'])
            categorized_channels[category].append(channel)
        
        # 生成验证后的M3U文件
        validated_content = self.generate_m3u_content(valid_channels, "已验证直播源")
        with open('outputs/full_validated.m3u', 'w', encoding='utf-8') as f:
            f.write(validated_content)
        
        # 生成分类文件
        for category, channels in categorized_channels.items():
            if channels:
                category_content = self.generate_m3u_content(channels, f"{category}频道")
                with open(f'outputs/{category}.m3u', 'w', encoding='utf-8') as f:
                    f.write(category_content)
        
        # 保存日志
        with open('logs/latest_update.log', 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.log_messages))
        
        # 统计信息
        end_time = time.time()
        duration = end_time - start_time
        validity_ratio = len(valid_channels) / len(quality_channels) if len(quality_channels) > 0 else 0
        
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
        
        # 更新README文件
        self.update_readme(stats)
        
        self.log(f"=== 更新完成！耗时: {duration:.2f}秒 ===")
        self.log(f"统计信息: {stats}")

if __name__ == "__main__":
    updater = IPTVUpdater()
    updater.run()    
    def fetch_source(self, url, timeout=15):
        """获取单个源"""
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
        """解析M3U内容"""
        channels = []
        current_channel = {}
        lines = content.splitlines()
        
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
                
            if line.startswith('#EXTINF'):
                # 解析频道信息
                current_channel = {'raw_extinf': line}
                # 提取频道名称
                name_match = re.search(r',(?P<name>.*)$', line)
                if name_match:
                    current_channel['name'] = name_match.group('name').strip()
                else:
                    current_channel['name'] = f"Unknown_{i}"
                    
            elif line.startswith('http://') or line.startswith('https://') or line.startswith('rtsp://') or line.startswith('rtmp://'):
                if current_channel:
                    current_channel['url'] = line
                    current_channel['source'] = source_url
                    channels.append(current_channel)
                    current_channel = {}
        
        self.log(f"从该源解析出 {len(channels)} 个频道")
        return channels
    
    def is_url_accessible(self, url, timeout=8):
        """检查URL是否可访问"""
        try:
            # 只发送HEAD请求检查，节省时间和带宽
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            return response.status_code in [200, 302, 301]
        except:
            try:
                # 如果HEAD失败，尝试GET但只读取头信息
                response = requests.get(url, timeout=timeout, stream=True)
                return response.status_code == 200
            except:
                return False
    
    def categorize_channel(self, channel_name):
        """分类频道"""
        name_lower = channel_name.lower()
        
        # 央视分类
        cctv_keywords = ['cctv', '央视', '中央']
        if any(keyword in name_lower for keyword in cctv_keywords):
            return 'cctv'
        
        # 卫视分类
        satellite_keywords = ['卫视', 'tvb', '凤凰', '星空', '湖南', '浙江', '江苏', '东方', '北京']
        if any(keyword in name_lower for keyword in satellite_keywords):
            return 'satellite'
        
        # 地方台分类
        local_keywords = ['都市', '新闻', '民生', '公共', '教育', '少儿', '体育', '影视', '综艺']
        if any(keyword in name_lower for keyword in local_keywords):
            return 'local'
        
        # 国际频道
        international_keywords = ['bbc', 'cnn', 'nhk', 'fox', 'hbo', 'disney', 'discovery', '国家地理']
        if any(keyword in name_lower for keyword in international_keywords):
            return 'international'
        
        return 'other'
    
    def generate_m3u_content(self, channels, category=None):
        """生成M3U内容"""
        if category:
            header = f"""#EXTM3U
#EXTENC: UTF-8
# Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
# Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Category: {category.upper()}
# Total Channels: {len(channels)}
# For personal testing and research purposes only.

"""
        else:
            header = f"""#EXTM3U
#EXTENC: UTF-8
# Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
# Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Total Channels: {len(channels)}
# For personal testing and research purposes only.

"""
        
        content = header
        for channel in channels:
            content += f"{channel['raw_extinf']}\n{channel['url']}\n"
        
        return content
    
    def update_readme(self, stats):
        """更新README文件，添加直播源地址"""
        try:
            # 读取现有的README内容
            if not os.path.exists('README.md'):
                # 如果README不存在，创建一个新的
                with open('README.md', 'w', encoding='utf-8') as f:
                    f.write('# DailyIPTV 📺\n\n')
                readme_content = '# DailyIPTV 📺\n\n'
            else:
                with open('README.md', 'r', encoding='utf-8') as f:
                    readme_content = f.read()
            
            # 生成直播源地址部分
            base_url = f"https://raw.githubusercontent.com/{self.repository_owner}/{self.repository_name}/main/outputs"
            update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            live_sources_section = f"""
## 📡 直播源地址

以下是最新的直播源地址（最后更新: {update_time}）：

### 🌐 完整列表
- **M3U格式**: [{base_url}/full.m3u]({base_url}/full.m3u)
- 频道总数: {stats['total_channels']} 个
- 估计有效频道: {stats['estimated_valid_channels']} 个
- 有效性比例: {stats['validity_ratio']:.2%}

### 📺 分类列表
- **央视频道**: [{base_url}/cctv.m3u]({base_url}/cctv.m3u) ({stats['categories']['cctv']} 个频道)
- **卫视频道**: [{base_url}/satellite.m3u]({base_url}/satellite.m3u) ({stats['categories']['satellite']} 个频道)
- **地方台**: [{base_url}/local.m3u]({base_url}/local.m3u) ({stats['categories']['local']} 个频道)
- **国际频道**: [{base_url}/international.m3u]({base_url}/international.m3u) ({stats['categories']['international']} 个频道)
- **其他频道**: [{base_url}/other.m3u]({base_url}/other.m3u) ({stats['categories']['other']} 个频道)

### 📊 统计信息
- **更新耗时**: {stats['duration_seconds']} 秒
- **源尝试数**: {stats['sources_attempted']} 个
- **成功源数**: {stats['sources_successful']} 个
- **更新时间**: {stats['update_time']}

### 🚀 快速使用
在支持M3U的播放器（VLC、PotPlayer、Kodi等）中：
1. 打开"打开网络流"
2. 粘贴上述任意链接
3. 享受直播！

---

"""
            
            # 检查是否已经有直播源地址部分
            if '## 📡 直播源地址' in readme_content:
                # 替换现有的直播源部分
                pattern = r'## 📡 直播源地址.*?---'
                updated_readme = re.sub(pattern, live_sources_section.strip(), readme_content, flags=re.DOTALL)
            else:
                # 在文件开头添加直播源部分
                if readme_content.startswith('# DailyIPTV 📺'):
                    updated_readme = readme_content.replace('# DailyIPTV 📺', f'# DailyIPTV 📺{live_sources_section}')
                else:
                    updated_readme = f'# DailyIPTV 📺{live_sources_section}\n\n{readme_content}'
            
            # 写入更新后的README
            with open('README.md', 'w', encoding='utf-8') as f:
                f.write(updated_readme)
            
            self.log("README文件更新成功！")
            return True
            
        except Exception as e:
            self.log(f"更新README文件失败: {e}")
            import traceback
            self.log(f"详细错误: {traceback.format_exc()}")
            return False

    def generate_complete_playlist(self, all_channels):
        """生成一个完整的播放列表，包含所有频道"""
        self.log("生成完整播放列表...")
        
        # 按分类组织频道
        categorized = {
            'cctv': [], 'satellite': [], 'local': [], 
            'international': [], 'other': []
        }
        
        for channel in all_channels:
            category = self.categorize_channel(channel['name'])
            categorized[category].append(channel)
        
        # 生成完整M3U内容
        complete_content = self.generate_m3u_content(all_channels)
        
        # 确保输出目录存在
        os.makedirs('outputs', exist_ok=True)
        
        # 写入完整文件
        with open('outputs/full.m3u', 'w', encoding='utf-8') as f:
            f.write(complete_content)
        
        # 写入分类文件
        for category, channels in categorized.items():
            if channels:
                category_content = self.generate_m3u_content(channels, category)
                with open(f'outputs/{category}.m3u', 'w', encoding='utf-8') as f:
                    f.write(category_content)
        
        return categorized

    def run(self):
        """主运行函数"""
        start_time = time.time()
        self.log("=== 开始更新IPTV直播源 ===")
        
        # 确保目录存在
        os.makedirs('outputs', exist_ok=True)
        os.makedirs('logs', exist_ok=True)
        
        # 加载配置
        self.sources_config = self.load_sources()
        
        # 获取所有源
        all_channels = []
        successful_sources = 0
        
        # 处理主要源
        sources_to_try = self.sources_config.get('sources', [])
        backup_sources = self.sources_config.get('backup_sources', [])
        
        self.log(f"尝试 {len(sources_to_try)} 个主要源和 {len(backup_sources)} 个备用源")
        
        for source_url in sources_to_try:
            content = self.fetch_source(source_url)
            if content:
                channels = self.parse_m3u(content, source_url)
                all_channels.extend(channels)
                successful_sources += 1
                time.sleep(1)  # 礼貌延迟
        
        # 如果主要源失败，尝试备用源
        if successful_sources == 0 and backup_sources:
            self.log("主要源全部失败，尝试备用源...")
            for backup_url in backup_sources:
                content = self.fetch_source(backup_url)
                if content:
                    channels = self.parse_m3u(content, backup_url)
                    all_channels.extend(channels)
                    successful_sources += 1
                    time.sleep(1)
        
        if not all_channels:
            self.log("错误：无法从任何源获取频道数据")
            return
        
        # 去重：基于URL去重
        unique_channels = {}
        for channel in all_channels:
            url = channel['url']
            if url not in unique_channels:
                unique_channels[url] = channel
        
        unique_channels_list = list(unique_channels.values())
        self.log(f"去重后频道数量: {len(unique_channels_list)}")
        
        # 验证频道有效性（抽样验证，避免耗时过长）
        self.log("开始验证频道有效性（抽样检查）...")
        valid_count = 0
        sample_size = min(100, len(unique_channels_list))
        
        for i, channel in enumerate(unique_channels_list[:sample_size]):
            if self.is_url_accessible(channel['url']):
                valid_count += 1
            if (i + 1) % 10 == 0 or (i + 1) == sample_size:
                self.log(f"已验证 {i+1}/{sample_size} 个频道")
        
        # 计算有效性比例
        validity_ratio = valid_count / sample_size if sample_size > 0 else 0
        estimated_valid_count = int(len(unique_channels_list) * validity_ratio)
        self.log(f"有效性抽样比例: {validity_ratio:.2%}, 估计有效频道: {estimated_valid_count}")
        
        # 生成完整的播放列表
        categorized_channels = self.generate_complete_playlist(unique_channels_list)
        
        # 统计信息
        end_time = time.time()
        duration = end_time - start_time
        
        stats = {
            'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'duration_seconds': round(duration, 2),
            'sources_attempted': len(sources_to_try) + len(backup_sources),
            'sources_successful': successful_sources,
            'total_channels': len(unique_channels_list),
            'estimated_valid_channels': estimated_valid_count,
            'validity_ratio': validity_ratio,
            'categories': {k: len(v) for k, v in categorized_channels.items()}
        }
        
        # 保存统计信息
        with open('outputs/stats.json', 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        
        # 保存日志
        with open('logs/latest_update.log', 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.log_messages))
        
        # 更新README文件
        self.log("正在更新README文件...")
        update_success = self.update_readme(stats)
        if update_success:
            self.log("README文件更新成功！")
        else:
            self.log("README文件更新失败！")
        
        self.log(f"=== 更新完成！耗时: {duration:.2f}秒 ===")
        self.log(f"统计信息: {json.dumps(stats, ensure_ascii=False, indent=2)}")
        
        # 输出文件信息
        self.log("生成的文件:")
        for file in os.listdir('outputs'):
            if file.endswith('.m3u'):
                file_path = os.path.join('outputs', file)
                file_size = os.path.getsize(file_path)
                self.log(f"  - {file}: {file_size} bytes")

if __name__ == "__main__":
    updater = IPTVUpdater()
    updater.run()
