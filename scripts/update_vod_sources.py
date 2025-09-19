#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import json
import re
from datetime import datetime
import time
import os

class VODUpdater:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        self.all_vod_items = []
        self.log_messages = []
        self.repository_owner = os.environ.get('GITHUB_REPOSITORY_OWNER', 'your-username')
        self.repository_name = os.environ.get('GITHUB_REPOSITORY', 'DailyIPTV').split('/')[-1]
        
    def log(self, message):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_message = f"[{timestamp}] {message}"
        print(log_message)
        self.log_messages.append(log_message)
        
    def load_vod_sources(self):
        """加载点播源列表"""
        try:
            with open('scripts/vod_sources_list.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            self.log(f"加载点播源列表失败: {e}")
            return {"vod_sources": []}
    
    def fetch_source(self, url, timeout=15):
        """获取单个源"""
        try:
            self.log(f"正在获取点播源: {url}")
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
        items = []
        current_item = {}
        lines = content.splitlines()
        
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
                
            if line.startswith('#EXTINF'):
                # 解析影片信息
                current_item = {
                    'raw_extinf': line,
                    'type': 'vod',
                    'source': source_url
                }
                # 提取名称
                name_match = re.search(r',(?P<name>.*)$', line)
                if name_match:
                    current_item['name'] = name_match.group('name').strip()
                else:
                    current_item['name'] = f"VOD_Unknown_{i}"
                
                # 提取分组信息
                group_match = re.search(r'group-title="([^"]*)"', line)
                if group_match:
                    current_item['group'] = group_match.group(1)
                else:
                    current_item['group'] = '未知分类'
                
                # 提取logo
                logo_match = re.search(r'tvg-logo="([^"]*)"', line)
                if logo_match:
                    current_item['logo'] = logo_match.group(1)
                    
            elif (line.startswith('http://') or line.startswith('https://') or 
                  line.startswith('rtsp://') or line.startswith('rtmp://')):
                if current_item:
                    current_item['url'] = line
                    items.append(current_item)
                    current_item = {}
        
        self.log(f"从该源解析出 {len(items)} 个点播项目")
        return items
    
    def categorize_vod(self, vod_name, vod_group=None):
        """分类点播内容"""
        name_lower = vod_name.lower()
        group_lower = (vod_group or '').lower()
        
        # 电影分类
        movie_keywords = ['电影', 'movie', '影院', '剧场', '大片', 'film', 'cinema']
        if any(keyword in name_lower for keyword in movie_keywords) or any(keyword in group_lower for keyword in movie_keywords):
            return 'movie'
        
        # 电视剧分类
        tv_keywords = ['电视剧', 'tv', '剧集', '连续剧', '美剧', '韩剧', '日剧', 'drama', 'series']
        if any(keyword in name_lower for keyword in tv_keywords) or any(keyword in group_lower for keyword in tv_keywords):
            return 'tv'
        
        # 综艺分类
        variety_keywords = ['综艺', '娱乐', '真人秀', '选秀', '脱口秀', 'variety', 'show']
        if any(keyword in name_lower for keyword in variety_keywords) or any(keyword in group_lower for keyword in variety_keywords):
            return 'variety'
        
        # 动漫分类
        anime_keywords = ['动漫', '动画', '卡通', 'anime', 'cartoon', '动漫']
        if any(keyword in name_lower for keyword in anime_keywords) or any(keyword in group_lower for keyword in anime_keywords):
            return 'anime'
        
        # 纪录片分类
        documentary_keywords = ['纪录片', '纪实', 'documentary', 'docu']
        if any(keyword in name_lower for keyword in documentary_keywords) or any(keyword in group_lower for keyword in documentary_keywords):
            return 'documentary'
        
        return 'other'
    
    def generate_m3u_content(self, items, category=None):
        """生成M3U内容"""
        if category:
            header = f"""#EXTM3U
#EXTENC: UTF-8
# Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
# Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Type: 点播
# Category: {category.upper()}
# Total Items: {len(items)}
# For personal testing and research purposes only.

"""
        else:
            header = f"""#EXTM3U
#EXTENC: UTF-8
# Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
# Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Type: 点播
# Total Items: {len(items)}
# For personal testing and research purposes only.

"""
        
        content = header
        for item in items:
            content += f"{item['raw_extinf']}\n{item['url']}\n"
        
        return content
    
    def update_vod_list_json(self, vod_items):
        """更新点播列表JSON文件"""
        vod_list = []
        for item in vod_items:
            vod_list.append({
                'name': item['name'],
                'url': item['url'],
                'group': item.get('group', '未知分类'),
                'logo': item.get('logo', ''),
                'category': self.categorize_vod(item['name'], item.get('group')),
                'source': item['source']
            })
        
        # 保存到JSON文件
        with open('outputs/vod_list.json', 'w', encoding='utf-8') as f:
            json.dump(vod_list, f, ensure_ascii=False, indent=2)
        
        self.log(f"已保存 {len(vod_list)} 个点播项目到 vod_list.json")
        return vod_list
    
    def generate_playlists(self, vod_items):
        """生成点播播放列表"""
        # 按分类组织点播内容
        vod_categorized = {
            'movie': [], 'tv': [], 'variety': [], 
            'anime': [], 'documentary': [], 'other': []
        }
        
        for vod in vod_items:
            category = self.categorize_vod(vod['name'], vod.get('group'))
            vod_categorized[category].append(vod)
        
        # 确保输出目录存在
        os.makedirs('outputs', exist_ok=True)
        
        # 生成完整点播文件
        vod_content = self.generate_m3u_content(vod_items)
        with open('outputs/vod_full.m3u', 'w', encoding='utf-8') as f:
            f.write(vod_content)
        
        # 生成分类点播文件
        for category, items in vod_categorized.items():
            if items:
                category_content = self.generate_m3u_content(items, category)
                with open(f'outputs/vod_{category}.m3u', 'w', encoding='utf-8') as f:
                    f.write(category_content)
        
        return vod_categorized

    def run(self):
        """主运行函数"""
        start_time = time.time()
        self.log("=== 开始更新点播源 ===")
        
        # 确保目录存在
        os.makedirs('outputs', exist_ok=True)
        os.makedirs('logs', exist_ok=True)
        
        # 加载配置
        vod_config = self.load_vod_sources()
        vod_sources = vod_config.get('vod_sources', [])
        
        if not vod_sources:
            self.log("没有配置点播源，请检查 vod_sources_list.json")
            return
        
        self.log(f"找到 {len(vod_sources)} 个点播源")
        
        # 获取所有点播源
        all_vod_items = []
        successful_sources = 0
        
        for vod_url in vod_sources:
            content = self.fetch_source(vod_url)
            if content:
                vod_items = self.parse_m3u(content, vod_url)
                all_vod_items.extend(vod_items)
                successful_sources += 1
                time.sleep(1)  # 礼貌延迟
        
        if not all_vod_items:
            self.log("错误：无法从任何点播源获取数据")
            return
        
        # 去重处理
        unique_vod_items = {}
        for vod in all_vod_items:
            url = vod['url']
            if url not in unique_vod_items:
                unique_vod_items[url] = vod
        
        unique_vod_list = list(unique_vod_items.values())
        self.log(f"去重后点播项目数量: {len(unique_vod_list)}")
        
        # 生成播放列表和JSON文件
        vod_categorized = self.generate_playlists(unique_vod_list)
        vod_json_list = self.update_vod_list_json(unique_vod_list)
        
        # 统计信息
        end_time = time.time()
        duration = end_time - start_time
        
        stats = {
            'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'duration_seconds': round(duration, 2),
            'vod_sources_attempted': len(vod_sources),
            'vod_sources_successful': successful_sources,
            'total_vod_items': len(unique_vod_list),
            'vod_categories': {k: len(v) for k, v in vod_categorized.items()}
        }
        
        # 保存统计信息
        with open('outputs/vod_stats.json', 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        
        # 保存日志
        with open('logs/vod_update.log', 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.log_messages))
        
        self.log(f"=== 点播源更新完成！耗时: {duration:.2f}秒 ===")
        self.log(f"统计信息: {json.dumps(stats, ensure_ascii=False, indent=2)}")
        
        # 输出文件信息
        self.log("生成的文件:")
        vod_files = [f for f in os.listdir('outputs') if f.startswith('vod_')]
        for file in vod_files:
            file_path = os.path.join('outputs', file)
            file_size = os.path.getsize(file_path)
            self.log(f"  - {file}: {file_size} bytes")

if __name__ == "__main__":
    updater = VODUpdater()
    updater.run()
