# -*- coding: utf-8 -*-
import feedparser
import json
import os
from qbittorrent import Client
import google.generativeai as genai
import time
import re
from json.decoder import JSONDecodeError
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs 
import base64
from datetime import datetime, timedelta, timezone 

# --- 配置及文件路径 ---
CONFIG_FILE = 'config.json'
SEEN_TORRENTS_FILE = 'seen_torrents.json'
RSS_LAST_UPDATE_FILE = 'rss_last_update.json' 
AI_ANALYZED_ENTRIES_FILE = 'ai_analyzed_entries.json' 

# --- 全局变量和客户端实例 ---
CONFIG = {}
SEEN_TORRENTS = set()
RSS_LAST_UPDATE_TIMES = {} 
QB_CLIENT = None
GEMINI_MODEL = None        
GEMINI_METADATA_MODEL = None 
CHAT_SESSION = None 
ALL_AI_SEARCHABLE_ENTRIES = [] # 存储AI搜索所需信息的轻量级列表: [{unique_id, title, published_parsed, metadata}]
FULL_ENTRY_DETAILS_MAP = {} # 存储完整条目信息（包括actual_download_link等），以unique_id为键，供按需查询
LAST_SEARCH_RESULTS = [] # 存储上次搜索结果的 unique_id 列表，用于分页和下载


# --- 辅助函数：加载/保存配置和已处理的种子 ---
def load_config():
    global CONFIG
    if not os.path.exists(CONFIG_FILE):
        print(f"错误：配置文件 '{CONFIG_FILE}' 不存在。请根据示例创建并填写。")
        exit()
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        try:
            CONFIG = json.load(f)
        except JSONDecodeError as e:
            print(f"错误：配置文件 '{CONFIG_FILE}' JSON 格式无效: {e}")
            exit()
        except Exception as e:
            print(f"错误：加载配置文件 '{CONFIG_FILE}' 时发生未知错误: {e}")
            exit()

def load_seen_torrents():
    global SEEN_TORRENTS
    if not os.path.exists(SEEN_TORRENTS_FILE):
        SEEN_TORRENTS = set()
        return

    try:
        if os.path.getsize(SEEN_TORRENTS_FILE) == 0:
             SEEN_TORRENTS = set()
             return
             
        with open(SEEN_TORRENTS_FILE, 'r', encoding='utf-8') as f:
           data = json.load(f)
           SEEN_TORRENTS = set(data) if isinstance(data, list) else set()
           
    except JSONDecodeError:
        print(f"警告: 无法解析文件 '{SEEN_TORRENTS_FILE}' (文件为空或JSON格式错误)。将返回空集合并继续。")
        SEEN_TORRENTS = set()
    except Exception as e:
         print(f"警告: 读取文件 '{SEEN_TORRENTS_FILE}' 发生错误: {e}。将返回空集合并继续。")
         SEEN_TORRENTS = set()

def save_seen_torrents():
    with open(SEEN_TORRENTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(SEEN_TORRENTS), f, ensure_ascii=False, indent=4)

def load_rss_last_update_times():
    global RSS_LAST_UPDATE_TIMES
    if not os.path.exists(RSS_LAST_UPDATE_FILE):
        RSS_LAST_UPDATE_TIMES = {}
        return
    try:
        with open(RSS_LAST_UPDATE_FILE, 'r', encoding='utf-8') as f:
            RSS_LAST_UPDATE_TIMES = json.load(f)
            for feed_name, timestamp_str in RSS_LAST_UPDATE_TIMES.items():
                if isinstance(timestamp_str, str):
                    RSS_LAST_UPDATE_TIMES[feed_name] = datetime.fromisoformat(timestamp_str)
    except JSONDecodeError:
        print(f"警告: 无法解析文件 '{RSS_LAST_UPDATE_FILE}' (文件为空或JSON格式错误)。将返回空字典。")
        RSS_LAST_UPDATE_TIMES = {}
    except Exception as e:
        print(f"警告: 读取文件 '{RSS_LAST_UPDATE_FILE}'发生错误: {e}。将返回空字典。")
        RSS_LAST_UPDATE_TIMES = {}

def save_rss_last_update_times():
    times_to_save = {k: v.isoformat() if isinstance(v, datetime) else v for k, v in RSS_LAST_UPDATE_TIMES.items()}
    with open(RSS_LAST_UPDATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(times_to_save, f, ensure_ascii=False, indent=4)

# --- 修正：加载/保存AI分析过的条目，并构建内存中的数据结构 ---
def load_ai_analyzed_entries():
    global ALL_AI_SEARCHABLE_ENTRIES, FULL_ENTRY_DETAILS_MAP
    ALL_AI_SEARCHABLE_ENTRIES = []
    FULL_ENTRY_DETAILS_MAP = {} 

    if not os.path.exists(AI_ANALYZED_ENTRIES_FILE):
        return
    try:
        with open(AI_ANALYZED_ENTRIES_FILE, 'r', encoding='utf-8') as f:
            loaded_entries = json.load(f)
            for entry_data in loaded_entries:
                # 转换 published_parsed 为 datetime 对象
                if entry_data.get('published_parsed') and isinstance(entry_data['published_parsed'], list):
                    try: 
                        entry_data['published_parsed'] = datetime(*entry_data['published_parsed'][:6])
                    except: 
                        entry_data['published_parsed'] = None
                
                entry_unique_id = entry_data.get('infohash')
                if not entry_unique_id:
                    entry_unique_id = entry_data.get('original_link')
                if not entry_unique_id: 
                    continue

                # 存储完整数据到映射表
                FULL_ENTRY_DETAILS_MAP[entry_unique_id] = entry_data 

                # 存储轻量级数据到 AI 可搜索列表
                ALL_AI_SEARCHABLE_ENTRIES.append({
                    "unique_id": entry_unique_id, 
                    "title": entry_data.get('title'),
                    "published_parsed": entry_data.get('published_parsed'),
                    "metadata": entry_data.get('metadata', {})
                })
    except JSONDecodeError:
        print(f"警告: 无法解析文件 '{AI_ANALYZED_ENTRIES_FILE}' (文件为空或JSON格式错误)。将返回空列表。")
        ALL_AI_SEARCHABLE_ENTRIES = []
        FULL_ENTRY_DETAILS_MAP = {}
    except Exception as e:
        print(f"警告: 读取文件 '{AI_ANALYZED_ENTRIES_FILE}' 发生错误: {e}。将返回空列表。")
        ALL_AI_SEARCHABLE_ENTRIES = []
        FULL_ENTRY_DETAILS_MAP = {}

def save_ai_analyzed_entries():
    # 修正：在保存前，对 FULL_ENTRY_DETAILS_MAP 中的每个条目进行深拷贝并转换 published_parsed
    entries_to_save_processed = []
    for entry in FULL_ENTRY_DETAILS_MAP.values():
        saved_entry_copy = entry.copy() # 创建副本，不修改原始数据
        if isinstance(saved_entry_copy.get('published_parsed'), datetime):
            # 转换为 time.struct_time 的形式，它是可JSON序列化的列表
            saved_entry_copy['published_parsed'] = saved_entry_copy['published_parsed'].timetuple()[:6] 
        entries_to_save_processed.append(saved_entry_copy)

    with open(AI_ANALYZED_ENTRIES_FILE, 'w', encoding='utf-8') as f:
        json.dump(entries_to_save_processed, f, ensure_ascii=False, indent=4)


# --- 健壮地提取 Infohash ---
def extract_infohash(link_candidate):
    if not link_candidate or not link_candidate.startswith('magnet:'):
        return None
    
    parsed_uri = urlparse(link_candidate)
    query_params = parse_qs(parsed_uri.query)
    
    if 'xt' in query_params:
        for xt_param in query_params['xt']:
            if xt_param.startswith('urn:btih:'):
                infohash = xt_param[len('urn:btih:'):]
                
                if re.fullmatch(r'[0-9a-fA-F]{40,64}', infohash):
                    return infohash.lower()

                elif re.fullmatch(r'[A-Z2-7]{32,52}', infohash, re.IGNORECASE):
                    try:
                        decoded_bytes = base64.b32decode(infohash.upper(), True)
                        return decoded_bytes.hex().lower()
                    except Exception as e:
                        print(f"警告:无法解码 Base32 infohash '{infohash}': {e}")
                        return None
    return None

# --- 从 RSS entry 或网页中提取实际下载链接 ---
def get_actual_download_link(entry):
    original_link = entry.link
    
    if hasattr(entry, 'enclosures') and entry.enclosures:
        for enc in entry.enclosures:
            if enc.href:
                if enc.type == 'application/x-bittorrent' or enc.href.startswith('magnet:'):
                    return enc.href
    
    if original_link and original_link.startswith('magnet:'):
        return original_link
    elif original_link and "share.dmhy.org/topics/view/" in original_link:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari=537.36'
            }
            response = requests.get(original_link, headers=headers, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            magnet_links_on_page = soup.find_all('a', href=re.compile(r'^magnet:'))
            if magnet_links_on_page:
                return magnet_links_on_page[0]['href']
            else:
                torrent_links_on_page = soup.find_all('a', href=re.compile(r'\.torrent$'))
                if torrent_links_on_page:
                    relative_path = torrent_links_on_page[0]['href']
                    return urljoin(original_link, relative_path)
        except requests.exceptions.RequestException as req_e:
            pass 
        except Exception as parse_e:
            pass 
    
    return original_link

# --- qBittorrent 任务添加与验证 ---
def add_and_verify_torrent(link, save_path, tags, title, unique_id):
    """
    添加 torrent 到 qBittorrent 并验证是否成功。
    返回 True if successful, False otherwise.
    """
    if CONFIG['dry_run']:
        print(f"  (模拟运行) 将下载 '{title}' 到 '{save_path}'，标签: {tags}")
        return True

    try:
        print(f"  发送下载任务到qBittorrent: {title}")
        QB_CLIENT.download_from_link(
            link,
            savepath=save_path,
            label=','.join(tags) if tags else None # 修正：使用 label 参数
        )
        
        added_successfully = False
        time.sleep(2) 

        all_torrents = QB_CLIENT.torrents()
        torrent_infohash = extract_infohash(link)

        if torrent_infohash:
            for torrent in all_torrents:
                if torrent['hash'] == torrent_infohash:
                    added_successfully = True
                    break
            
            if added_successfully:
                print(f"  任务添加成功: {title}")
            else:
                print(f"  警告: Torrent '{title}' (infohash: {torrent_infohash}) 未能在 qBittorrent 列表中找到。")
                print(f"    请手动检查 qBittorrent Web UI 或日志，确认是否添加成功或被拒绝。")
                added_successfully = False 
        else:
            print(f"  警告: 无法从链接 '{link}' 提取infohash，无法精确验证添加。")
            print(f"    请手动检查 qBittorrent Web UI，确认 '{title}' 是否被添加。")
            added_successfully = True

        return added_successfully

    except Exception as add_e:
        print(f"  添加下载任务失败 '{title}': {add_e}")
        return False

# --- Gemini AI 交互函数及工具定义 ---

# AI 辅助信息提取函数 (使用独立的模型实例，并引入批量处理和速率限制)
def extract_metadata_with_gemini_batch(entries_data_batch): 
    if not entries_data_batch:
        return []

    prompt_parts = []
    prompt_parts.append(f"""
你是一个智能的元数据提取助手。你的任务是从以下给定的多个资源标题和描述中，逐一提取结构化信息，并输出一个 JSON 数组。数组的每个元素对应一个资源，包含以下字段：

1.  **title**: 资源的完整标题。
2.  **media_type**: 媒体类型，可选值包括 "动漫剧集", "动漫电影", "动漫音乐", "游戏", "软件", "其他"。
    *   如果标题中包含“TVアニメ”、“剧场版”、“OVA”、“动画”、“番剧”等，media_type 可能是“动漫剧集”或“动漫电影”。
    *   如果标题中包含“OP”、“ED”、“OST”、“专辑”、“单曲”、“音楽”、“MUSIC”、“BGM”、“SOUNDTRACK”等，media_type 可能是“动漫音乐”。
    *   如果标题中包含“游戏”、“Game”、“PC GAME”、“PS4”、“Switch”等，media_type 可能是“游戏”。
    *   如果标题同时包含动漫和音乐关键词，**优先判断为“动漫音乐”**。
3.  **anime_title**: 如果 media_type 是“动漫剧集”、“动漫电影”或“动漫音乐”，则提取动漫的完整标题。如果有别名，请尽可能识别并包含最常见的中文或日文名称。例如，“ウマ娘 プリティーダービー”请识别为“赛马娘”，“前橋ウィッチーズ”请识别为“前桥魔女”。如果无法识别，设为 null。
4.  **song_type**: 如果 media_type 是“动漫音乐”，歌曲类型，可选值包括 "OP", "ED", "插入歌", "OST", "专辑", "单曲", "VGM", "其他"。如果无法识别，设为 null。
5.  **quality**: 音质，可选值包括 "FLAC", "320K", "Hi-Res"。如果包含 "48kHz/24bit" 或 "96kHz/24bit" 等，请识别为"Hi-Res"。如果无法识别，设为 null。
6.  **artists**: 如果 media_type 是“动漫音乐”，则为艺术家/歌手/声优列表，如果能识别到的话。如果无法识别，设为 null。
7.  **resolution**: 如果 media_type 是“动漫剧集”或“动漫电影”，则为视频分辨率，如 "1080p", "720p", "4K"。如果无法识别，设为 null。

如果某个字段无法从标题或描述中提取，请将其值设为 null。
请严格遵守 JSON 数组格式输出，不要包含任何额外文字或解释。
务必确保数组中的每个元素都包含上述所有字段，即使值为null。
务必确保返回的JSON数组中的元素顺序与输入资源列表的顺序严格一致。

示例输出:
[
  {{
      "title": "[Hi-Res][250609]TVアニメ『前桥魔女』...",
      "media_type": "动漫音乐",
      "anime_title": "前桥魔女",
      "song_type": "插入歌",
      "quality": "Hi-Res",
      "artists": ["前桥ウィッチーズ"],
      "resolution": null
  }},
  {{
      "title": "[喵萌奶茶屋&LoliHouse] 末日后酒店 / Apocalypse Hotel - 09 ...",
      "media_type": "动漫剧集",
      "anime_title": "末日后酒店",
      "song_type": null,
      "quality": null,
      "artists": null,
      "resolution": "1080p"
  }}
]

以下是需要分析的资源列表：
""")

    for i, entry_data in enumerate(entries_data_batch):
        prompt_parts.append(f"""
----- 资源 {i+1} -----
资源标题: {entry_data['title']}
资源描述: {entry_data.get('description', '无描述')}
""")

    full_prompt = "".join(prompt_parts)

    retries = 3 
    for attempt in range(retries):
        try:
            response = GEMINI_METADATA_MODEL.generate_content(
                full_prompt,
                generation_config=genai.GenerationConfig(response_mime_type="application/json")
            )
            parsed_results = json.loads(response.text)
            
            if isinstance(parsed_results, list) and len(parsed_results) == len(entries_data_batch):
                return parsed_results
            else:
                print(f"  警告: Gemini 返回的JSON格式不符合预期，批次中首条标题: {entries_data_batch[0]['title'][:30]}... 尝试 {attempt + 1}/{retries}。返回: {response.text[:100]}...")
                if "429" not in response.text: 
                    time.sleep(2) 
                continue 

        except Exception as e:
            if "429" in str(e): 
                retry_delay_for_429 = 5 * (attempt + 1) 
                print(f"  警告: Gemini 提取元数据速率限制，批次中首条标题: {entries_data_batch[0]['title'][:30]}... 尝试 {attempt + 1}/{retries}。等待 {retry_delay_for_429} 秒后重试。")
                time.sleep(retry_delay_for_429) 
            else:
                print(f"  警告: Gemini 提取元数据失败，批次中首条标题: {entries_data_batch[0]['title'][:30]}... 错误: {type(e).__name__}: {e}")
                break 
    return [{}] * len(entries_data_batch) 

# RSS 搜索工具的实现
def search_rss_items(anime_title=None, artist=None, song_type=None, quality=None, media_type=None, limit=20, only_unseen=False, random_recommend=False, offset=0): 
    """
    在已加载的所有资源中搜索匹配条件的条目。
    Args:
        anime_title (str): 动漫标题，支持部分匹配和别名理解。
        artist (str): 艺术家或歌手的名称。
        song_type (str): 歌曲类型，如 "OP", "ED", "插入歌", "OST", "专辑", "单曲", "VGM"。
        quality (str): 音质，如 "FLAC", "320K", "Hi-Res"。
        media_type (str): 媒体类型，如 "动漫剧集", "动漫电影", "动漫音乐", "游戏", "软件", "其他"。
        limit (int): 返回结果的最大数量。
        only_unseen (bool): 是否只返回未曾处理过的资源。默认为 False。
        random_recommend (bool): 如果为 True，则忽略其他条件，随机推荐。默认为 False。
        offset (int): 搜索结果的起始偏移量，用于分页。默认为 0。
    Returns:
        dict: 包含 "results" (匹配的资源列表), "total_results" (总数), "offset" (当前偏移量) 和 "limit" (当前限制)。
    """
    global LAST_SEARCH_RESULTS

    print("AI: 正在从已加载的资源中筛选结果...")

    # 修正：将 limit 和 offset 强制转换为 int 类型，防止TypeError
    limit = int(limit) if limit is not None else 20
    offset = int(offset) if offset is not None else 0

    all_matching_candidates = []
    for entry_data in ALL_AI_SEARCHABLE_ENTRIES: 
        
        metadata = entry_data.get("metadata", {})
        
        if media_type:
            extracted_media_type = metadata.get("media_type")
            if not extracted_media_type or media_type.lower() not in extracted_media_type.lower():
                continue

        if anime_title:
            extracted_anime_title = metadata.get("anime_title")
            if not extracted_anime_title: 
                continue
            
            user_title_lower = anime_title.lower()
            extracted_title_lower = extracted_anime_title.lower()
            original_title_lower = entry_data["title"].lower() 

            if not (user_title_lower in extracted_title_lower or \
                    extracted_title_lower in user_title_lower or \
                    user_title_lower in original_title_lower):
                continue

        if artist:
            extracted_artists = metadata.get("artists", [])
            if not extracted_artists or not any(artist.lower() in a.lower() for a in extracted_artists):
                continue
        
        if song_type:
                extracted_song_type = metadata.get("song_type")
                if not extracted_song_type or song_type.lower() not in extracted_song_type.lower():
                    continue

        if quality:
            extracted_quality = metadata.get("quality")
            if not extracted_quality or quality.lower() not in extracted_quality.lower():
                continue
        
        if only_unseen:
            entry_unique_id = entry_data["unique_id"] 
            if entry_unique_id in SEEN_TORRENTS:
                continue

        all_matching_candidates.append(entry_data)
    
    total_results = len(all_matching_candidates) 

    if random_recommend and all_matching_candidates:
        import random
        random.shuffle(all_matching_candidates)
        filtered_results = all_matching_candidates[:limit]
    else:
        # 修正：当没有任何过滤条件（除了 offset 和 limit），并且没有要求随机推荐
        # 则默认按时间倒序排序所有候选者，以实现“最新资源”的默认概览
        # 如果有任何过滤条件，但没有明确排序要求，也保持按时间倒序
        if not (anime_title or artist or song_type or quality or media_type or random_recommend):
            all_matching_candidates.sort(key=lambda x: x['published_parsed'] if x['published_parsed'] else datetime.min, reverse=True)
            
        start_index = max(0, min(offset, total_results))
        end_index = min(start_index + limit, total_results)
        
        filtered_results = all_matching_candidates[start_index:end_index]
            
    LAST_SEARCH_RESULTS = filtered_results 
    
    return { 
        "results": [
            {
                "index": i + 1 + offset, 
                "title": res["title"],
                "unique_id": res["unique_id"] 
            } for i, res in enumerate(filtered_results)
        ],
        "total_results": total_results, 
        "offset": offset, 
        "limit": limit 
    }


def list_recent_animes_with_music(limit=5):
    """
    列出最近有更新音乐资源的动漫作品。
    Args:
        limit (int): 返回的最大动漫数量。
    Returns:
        list[dict]: 包含动漫名称和其最近音乐的摘要。
    """
    print("AI: 正在分析最近的动漫音乐资源...")
    
    # 修正：将 limit 强制转换为 int 类型
    limit = int(limit) if limit is not None else 5

    anime_music_map = {} 
    for entry_data in ALL_AI_SEARCHABLE_ENTRIES: 
        entry_unique_id = entry_data["unique_id"] 
        if entry_unique_id in SEEN_TORRENTS: 
            continue 
            
        metadata = entry_data.get("metadata", {})
        anime_title = metadata.get("anime_title")
        media_type = metadata.get("media_type")
        
        if anime_title and (media_type == "动漫音乐"): 
            if anime_title not in anime_music_map:
                anime_music_map[anime_title] = []
            anime_music_map[anime_title].append(entry_data)
    
    recent_animes = []
    sorted_animes_by_date = sorted(
        anime_music_map.items(), 
        key=lambda item: max(e['published_parsed'] if e.get('published_parsed') else datetime.min for e in item[1]), 
        reverse=True
    )

    for anime_title, entries in sorted_animes_by_date:
        entries.sort(key=lambda x: x['published_parsed'] if x.get('published_parsed') else datetime.min, reverse=True)
        
        music_summary = []
        for i, entry in enumerate(entries[:3]): 
            music_summary.append(f"《{entry['title']}》")
        
        recent_animes.append({
            "anime_title": anime_title,
            "music_count": len(entries),
            "latest_music_summary": music_summary
        })
        if len(recent_animes) >= limit:
            break
            
    return recent_animes

# --- 新增工具函数：获取资源概览 ---
def get_overall_resource_summary(limit_examples=5):
    """
    提供已加载的RSS资源库的整体概览。
    Args:
        limit_examples (int): 返回随机示例资源的数量。
    Returns:
        dict: 包含总资源数和一些随机示例资源标题。
    """
    print("AI: 正在统计资源概览...")
    # 修正：将 limit_examples 强制转换为 int 类型
    limit_examples = int(limit_examples) if limit_examples is not None else 5

    total_entries = len(ALL_AI_SEARCHABLE_ENTRIES)
    
    examples = []
    if total_entries > 0:
        import random
        unseen_candidates = [
            entry for entry in ALL_AI_SEARCHABLE_ENTRIES 
            if (entry['unique_id'] not in SEEN_TORRENTS)
        ]
        if len(unseen_candidates) > limit_examples:
            examples = random.sample(unseen_candidates, limit_examples)
        else:
            examples = unseen_candidates
    
    return {
        "total_resources": total_entries,
        "example_titles": [f"《{res['title']}》" for res in examples]
    }


# 定义 Gemini 可以使用的工具
TOOL_FUNCTIONS = [
    search_rss_items,
    list_recent_animes_with_music,
    get_overall_resource_summary 
]


# --- 主逻辑函数 ---
def main():
    global QB_CLIENT, GEMINI_MODEL, GEMINI_METADATA_MODEL, CHAT_SESSION, ALL_AI_SEARCHABLE_ENTRIES, FULL_ENTRY_DETAILS_MAP, LAST_SEARCH_RESULTS

    load_config()
    load_seen_torrents()
    load_rss_last_update_times() 
    load_ai_analyzed_entries() 
    
    qb_config = CONFIG['qbittorrent']
    gemini_config = CONFIG['gemini']

    print(f"脚本以 {'模拟运行模式' if CONFIG['dry_run'] else '实际运行模式'} 启动。")

    # 连接 qBittorrent 客户端
    try:
        QB_CLIENT = Client(qb_config['url'])
        QB_CLIENT.login(qb_config['username'], qb_config['password'])
        print(f"成功连接到 qBittorrent ({qb_config['url']}).")
    except Exception as e:
        print(f"连接或登录 qBittorrent 失败: {e}")
        print("请检查 qBittorrent Web UI 是否开启，以及配置文件中的 URL、用户名和密码是否正确。")
        exit()

    # 初始化 Gemini 模型和对话会话
    try:
        genai.configure(api_key=gemini_config['api_key'])
        
        # 1. 初始化用于对话和函数调用的主模型
        GEMINI_MODEL = genai.GenerativeModel(
            model_name=gemini_config['model_name'],
            tools=TOOL_FUNCTIONS 
        )
        
        # 2. 初始化用于元数据提取的独立模型 (不带工具，只用于生成JSON)
        GEMINI_METADATA_MODEL = genai.GenerativeModel(
            model_name=gemini_config['model_name'] 
        )

        CHAT_SESSION = GEMINI_MODEL.start_chat(history=[
            {"role": "user", "parts": "你好，请记住我是一个用户，你是一个能够搜索各种资源并辅助我下载的智能助手。你能够理解资源类型（动漫剧集、动漫电影、动漫音乐、游戏、软件等）、动漫名称的别名（如“赛马娘”指代“ウマ娘 プリティーダービー”），并识别歌曲类型、音质、视频分辨率等。"},
            {"role": "model", "parts": "好的，我明白。我将根据您的请求智能搜索各种资源，并协助您下载。"},
            {"role": "user", "parts": "当我询问“rss中都有哪些资源”、“你都加载了啥数据”、“有什么资源”这类宽泛问题时，请你直接调用 `get_overall_resource_summary` 工具来告诉我总数和一些随机示例，而**不要**反问我细致的条件。当我没有明确指定搜索条件时，你也可以直接执行一个默认搜索（例如，最近的或随机的）。当我问“最近有什么动漫”或“某个动漫有什么音乐”时，请你分析已有的资源数据来回答。在列出搜索结果时，请以简洁的“序号. 资源标题”格式呈现，不要包含链接，并询问我是否需要下载。如果结果数量很多，请列出前20项，并告诉我总共有多少项结果，以及如何查看更多（例如，输入'下一页'或'查看更多'）。如果我输入'download <序号>'或'download <序号1>,<序号2>'，你将直接执行下载。"},
            {"role": "model", "parts": "好的，我明白了。我将优化我的搜索和推荐方式，直接提供结果概要，并引导您下载。请问您想找些什么？例如，可以告诉我资源类型、动漫名称、歌手、歌曲类型、音质、分辨率等。您也可以问我“最近有什么新动漫”或“某个动漫有什么音乐”。"},
        ]) 
        print("\nAI 助手已启动，请开始提问！(输入 'exit' 退出, 'download #<num>' 下载)")
    except Exception as e:
        print(f"初始化 Gemini AI 失败: {e}")
        print("请检查配置文件中的 Gemini API Key 和模型名称。")
        exit()

    # --- 核心：在对话开始前预加载 RSS 数据并进行AI元数据提取 ---
    print("\n--- 预加载所有 RSS Feed 并进行智能分析（初次运行可能需要较长时间）---")
    
    if not GEMINI_METADATA_MODEL:
        print("错误: Gemini 元数据提取模型未初始化。请检查初始化步骤。")
        exit()

    new_rss_update_times = {} 

    existing_unique_ids = {entry['unique_id'] for entry in ALL_AI_SEARCHABLE_ENTRIES}
    
    newly_analyzed_count = 0

    for feed_name, feed_url in CONFIG['rss_feeds'].items():
        print(f"  正在加载 {feed_name} ({feed_url})...")
        try:
            feed_entries_to_analyze = [] 
            latest_entry_timestamp_from_file = RSS_LAST_UPDATE_TIMES.get(feed_name) 
            
            feed = feedparser.parse(feed_url)
            if feed.bozo:
                print(f"  警告: RSS Feed '{feed_name}' 解析错误: {feed.bozo_exception}")
            
            current_feed_max_timestamp = None 
            
            for entry in feed.entries:
                entry_datetime = None
                if entry.get('published_parsed'):
                    entry_datetime = datetime(*entry['published_parsed'][:6])
                    if current_feed_max_timestamp is None or entry_datetime > current_feed_max_timestamp:
                        current_feed_max_timestamp = entry_datetime

                    if latest_entry_timestamp_from_file and entry_datetime <= latest_entry_timestamp_from_file:
                        continue 

                actual_download_link = get_actual_download_link(entry)
                infohash = extract_infohash(actual_download_link)
                
                entry_unique_id = infohash if infohash else entry.link
                
                if entry_unique_id in existing_unique_ids:
                    continue 

                feed_entries_to_analyze.append({
                    "title": entry.title,
                    "original_link": entry.link,
                    "description": entry.get('description', ''),
                    "actual_download_link": actual_download_link,
                    "infohash": infohash,
                    "published_parsed": entry.get('published_parsed')
                })

            print(f"  '{feed_name}' 原始RSS条目加载完成，共 {len(feed_entries_to_analyze)} 条新条目待AI分析。")

            batch_size = 20 
            for i in range(0, len(feed_entries_to_analyze), batch_size):
                batch_entries = feed_entries_to_analyze[i:i + batch_size]
                
                print(f"    - '{feed_name}' 正在分析批次 {i // batch_size + 1} / {len(feed_entries_to_analyze) // batch_size + (1 if len(feed_entries_to_analyze) % batch_size else 0)} (条目 {i + 1} - {min(i + batch_size, len(feed_entries_to_analyze))})")

                extracted_metadata_batch = extract_metadata_with_gemini_batch(batch_entries)
                
                for j, entry_data in enumerate(extracted_metadata_batch): 
                    metadata = entry_data 
                    if not metadata or not metadata.get('title'): 
                         print(f"      警告: 批次 {i // batch_size + 1} 中条目 {j+1} 元数据提取为空或不完整。")
                         metadata = {} 
                    
                    current_entry_unique_id = feed_entries_to_analyze[i+j].get('infohash') 
                    if not current_entry_unique_id:
                        current_entry_unique_id = feed_entries_to_analyze[i+j].get('original_link')

                    if current_entry_unique_id:
                        FULL_ENTRY_DETAILS_MAP[current_entry_unique_id] = {**feed_entries_to_analyze[i+j], "metadata": metadata}
                        ALL_AI_SEARCHABLE_ENTRIES.append({
                            "unique_id": current_entry_unique_id,
                            "title": feed_entries_to_analyze[i+j].get('title'),
                            "published_parsed": feed_entries_to_analyze[i+j].get('published_parsed'),
                            "metadata": metadata
                        })
                        newly_analyzed_count += 1 
                    else:
                        print(f"      警告: 条目 '{feed_entries_to_analyze[i+j].get('title')}' 无法生成唯一ID，跳过AI分析后的存储。")

            print(f"  '{feed_name}' AI分析完成，共 {len(feed_entries_to_analyze)} 条已分析。")
            
            if current_feed_max_timestamp:
                new_rss_update_times[feed_name] = current_feed_max_timestamp 

        except Exception as e:
            print(f"  错误：加载或分析 RSS Feed '{feed_name}' 失败: {e}")
    
    if newly_analyzed_count > 0:
        save_ai_analyzed_entries() 

    RSS_LAST_UPDATE_TIMES.update(new_rss_update_times) 
    save_rss_last_update_times()

    print(f"--- 所有 RSS Feed 预加载并分析完成，总共 {len(ALL_AI_SEARCHABLE_ENTRIES)} 个条目可供搜索。---")

    # --- 对话循环 ---
    while True:
        try:
            user_input = input("\n你: ")
            if user_input.lower().startswith('exit'):
                print("AI: 再见！")
                break

            if user_input.lower().startswith('download'): 
                try:
                    temp_str = user_input.lower().replace('download ', '').strip()
                    temp_str = temp_str.replace('#', '')
                    
                    selected_indices = []
                    for s in temp_str.split(','):
                        s_stripped = s.strip()
                        if s_stripped.isdigit():
                            selected_indices.append(int(s_stripped))
                    
                    if not selected_indices:
                        print("AI: 无效的下载指令格式。请使用 'download <序号>' 或 'download <序号1>,<序号2>'。")
                        continue 
                    
                    if not LAST_SEARCH_RESULTS:
                        print("AI: 请先进行搜索，然后选择要下载的资源。")
                        continue
                    
                    for idx in selected_indices:
                        if 1 <= idx <= len(LAST_SEARCH_RESULTS):
                            selected_search_result = LAST_SEARCH_RESULTS[idx - 1]
                            selected_unique_id = selected_search_result["unique_id"] 
                            
                            full_entry_data = FULL_ENTRY_DETAILS_MAP.get(selected_unique_id)
                            
                            if not full_entry_data:
                                print(f"AI: 错误：无法找到序号 #{idx} 对应的完整资源信息。请重试或搜索其他资源。")
                                continue

                            title = full_entry_data["title"]
                            actual_download_link = full_entry_data["actual_download_link"]
                            
                            if selected_unique_id in SEEN_TORRENTS: 
                                print(f"  资源 '{title}' 已在已处理列表中，跳过下载。")
                                continue

                            default_path = CONFIG.get('default_download_path', '/downloads/Others')
                            
                            # --- 修正：从 AI 提取的元数据中智能生成标签 ---
                            generated_tags = []
                            metadata = full_entry_data.get('metadata', {})
                            
                            if metadata.get('media_type'):
                                generated_tags.append(metadata['media_type'])
                            
                            if metadata.get('anime_title'):
                                cleaned_anime_title = re.sub(r'[^\w\s-]', '', metadata['anime_title']).strip()
                                if cleaned_anime_title:
                                    generated_tags.append(cleaned_anime_title[:50]) 

                            if metadata.get('song_type'):
                                generated_tags.append(metadata['song_type'])
                                
                            if metadata.get('quality'):
                                generated_tags.append(metadata['quality'])
                                
                            if metadata.get('artists') and isinstance(metadata['artists'], list):
                                for artist_name in metadata['artists'][:2]: 
                                    cleaned_artist_name = re.sub(r'[^\w\s-]', '', artist_name).strip()
                                    if cleaned_artist_name:
                                        generated_tags.append(cleaned_artist_name[:30])

                            if metadata.get('resolution'):
                                generated_tags.append(metadata['resolution'])

                            default_tags = generated_tags if generated_tags else ['自动下载']
                            default_tags = list(dict.fromkeys(default_tags))
                            # --- 修正结束 ---

                            print(f"\nAI: 准备下载 '{title}'...")
                            if add_and_verify_torrent(actual_download_link, default_path, default_tags, title, selected_unique_id): 
                                SEEN_TORRENTS.add(selected_unique_id) 
                                save_seen_torrents()
                            else:
                                print(f"AI: 下载 '{title}' 失败。请检查日志或手动下载。")
                        else:
                            print(f"AI: 序号 #{idx} 无效。请选择列表中的有效序号。")
                except ValueError:
                    print("AI: 无效的下载指令格式。请使用 'download <序号>' 或 'download <序号1>,<序号2>'。")
                except Exception as e:
                    print(f"AI: 处理下载指令时发生错误: {e}")
                continue 

            # 将用户输入发送给 Gemini
            response = CHAT_SESSION.send_message(user_input)

            # 处理 Gemini 的响应：健壮地遍历所有 parts
            printed_response = False
            for part in response.parts:
                if part.function_call:
                    tool_call = part.function_call
                    function_name = tool_call.name
                    args_dict = tool_call.args._asdict() if hasattr(tool_call.args, '_asdict') else dict(tool_call.args) 

                    if function_name == "search_rss_items":
                        print(f"AI: 正在执行搜索任务...")
                        
                        if 'limit' not in args_dict or args_dict['limit'] is None:
                            args_dict['limit'] = 20 
                        
                        if 'offset' not in args_dict or args_dict['offset'] is None:
                            args_dict['offset'] = 0 
                        
                        if not any(arg in args_dict and args_dict[arg] is not None for arg in ['anime_title', 'artist', 'song_type', 'quality', 'media_type', 'random_recommend']):
                            args_dict['random_recommend'] = False 
                        
                        search_results_dict = search_rss_items(**args_dict) 
                        results_for_ai = search_results_dict.get('results', []) 
                        total_results = search_results_dict.get('total_results', 0)
                        current_offset_after_search = search_results_dict.get('offset', 0)
                        current_limit_after_search = search_results_dict.get('limit', 0)

                        tool_response_part = genai.protos.Part(
                            function_response=genai.protos.FunctionResponse(
                                name="search_rss_items",
                                response={
                                    "results": results_for_ai, 
                                    "total_results": total_results,
                                    "current_offset": current_offset_after_search,
                                    "current_limit": current_limit_after_search
                                }
                            )
                        )
                        final_response_from_tool = CHAT_SESSION.send_message(tool_response_part)
                        
                        for final_part in final_response_from_tool.parts:
                            if final_part.text:
                                print("AI:", final_part.text)
                                printed_response = True
                                break 
                        
                        if not printed_response:
                            print("AI: 抱歉，搜索结果已返回，但我无法以文本形式呈现。")

                    elif function_name == "list_recent_animes_with_music": 
                        print(f"AI: 正在汇总最新的动漫音乐信息...")
                        args_dict = tool_call.args._asdict() if hasattr(tool_call.args, '_asdict') else dict(tool_call.args) 
                        results = list_recent_animes_with_music(**args_dict)

                        tool_response_part = genai.protos.Part(
                            function_response=genai.protos.FunctionResponse(
                                name="list_recent_animes_with_music",
                                response={"results": results}
                            )
                        )
                        final_response_from_tool = CHAT_SESSION.send_message(tool_response_part)
                        
                        for final_part in final_response_from_tool.parts:
                            if final_part.text:
                                print("AI:", final_part.text)
                                printed_response = True
                                break 
                        
                        if not printed_response:
                            print("AI: 抱歉，动漫音乐列表已返回，但我无法以文本形式呈现。")
                    elif function_name == "get_overall_resource_summary": 
                        print(f"AI: 正在统计资源概览...")
                        args_dict = tool_call.args._asdict() if hasattr(tool_call.args, '_asdict') else dict(tool_call.args) 
                        summary_results = get_overall_resource_summary(**args_dict)

                        tool_response_part = genai.protos.Part(
                            function_response=genai.protos.FunctionResponse(
                                name="get_overall_resource_summary",
                                response={
                                    "total_resources": summary_results["total_resources"], 
                                    "example_titles": summary_results["example_titles"]
                                }
                            )
                        )
                        final_response_from_tool = CHAT_SESSION.send_message(tool_response_part)
                        
                        for final_part in final_response_from_tool.parts:
                            if final_part.text:
                                print("AI:", final_part.text)
                                printed_response = True
                                break 
                        
                        if not printed_response:
                            print("AI: 抱歉，资源概览已返回，但我无法以文本形式呈现。")
                    else:
                        print(f"AI: 我不明白你想做什么，或者我没有执行 '{function_name}' 的工具。")
                elif part.text:
                    print("AI:", part.text)
                    printed_response = True
            
            if not printed_response:
                print("AI: 抱歉，我收到一个非文本或无法解析的响应。")

        except KeyboardInterrupt:
            print("\nAI: 收到中断信号，退出对话。")
            break
        except Exception as e:
            print(f"AI: 发生未知错误: {e}")
            print("AI: 请尝试重新开始对话。")

    if QB_CLIENT:
        try:
            pass 
        except Exception as e:
            print(f"退出 qBittorrent 登录时发生错误: {e}")

    print("\n脚本执行完毕。")

if __name__ == "__main__":
    main()