#!/usr/bin/env python3
"""
Rally News Scraper - German Edition
Only scrapes positive news from whitelisted German-language sources within last 48 hours
"""

import requests
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import time
import os

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')

# Strict whitelist - ONLY these German-language sources allowed
WHITELISTED_SOURCES = {
    'Berliner Zeitung',
    'Sueddeutsche Zeitung',
    'Berliner Morgenpost',
    'Die Zeit',
    'Tagesschau',
    'Spiegel',
    'FAZ',
    'Deutsche Welle',
    'ZDF',
    'ARD',
    'Deutschlandfunk',
    'Klimareporter',
    'Sport.de',
}

# RSS feeds for whitelisted German sources
RSS_FEEDS = {
    'Berliner Zeitung':    'https://www.berliner-zeitung.de/feed.xml',
    'Sueddeutsche Zeitung': 'https://rss.sueddeutsche.de/rss/Topthemen',
    'Berliner Morgenpost': 'https://www.morgenpost.de/rss.xml',
    'Die Zeit':            'https://newsfeed.zeit.de/index',
    'Tagesschau':          'https://www.tagesschau.de/xml/rss2',
    'Spiegel':             'https://www.spiegel.de/schlagzeilen/tops/index.rss',
    'FAZ':                 'https://www.faz.net/rss/aktuell/',
    'Deutsche Welle':      'https://rss.dw.com/rdf/rss-de-all',
    'ZDF':                 'https://www.zdf.de/rss/zdf/nachrichten.rss',
    'ARD':                 'https://www.tagesschau.de/inland/index~rss2.xml',
    'Deutschlandfunk':     'https://www.deutschlandfunk.de/die-nachrichten.353.de.rss',
    'Klimareporter':       'https://klimareporter.de/feed',
    'Sport.de':            'https://www.sport.de/rss/alle-sportmeldungen.rss',
}

# Valid categories (AI will categorize into these)
VALID_CATEGORIES = [
    'climate',        # Umwelt, Nachhaltigkeit, erneuerbare Energien
    'transportation', # Verkehr, Infrastruktur, Mobilität
    'ai',            # Technologie, Wissenschaft, Forschung, Innovation
    'business',      # Wirtschaft, Finanzen, Unternehmen, Startups
    'politics',      # Politik, Gesetze, Wahlen, Demokratie
    'entertainment', # Film, Musik, Sport, Kultur, TV
    'world',         # Internationale Nachrichten, Diplomatie
    'religion',      # Glaube, Spiritualität, religiöse Themen
    'arts'           # Kultur, Literatur, Museen, Theater
]

# Multi-model fallback (free models first, paid as fallback)
AI_MODELS = [
    'nvidia/llama-3.1-nemotron-70b-instruct',
    'poolside/laguna-70b-chat',
    'openai/gpt-4o-mini-2024-07-18',
    'minimax/minimax-01',
    'inclusionai/ring-flash-preview',
    'openai/o1-mini-2024-09-12',
    'google/gemini-2.0-flash-exp:free'  # Paid fallback
]

# ═══════════════════════════════════════════════════════════════
# RSS PARSER (stdlib only — no feedparser dependency)
# ═══════════════════════════════════════════════════════════════

def parse_feed(url, timeout=15):
    """Fetch and parse an RSS or Atom feed; return list of entry dicts."""
    try:
        resp = requests.get(url, timeout=timeout, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; RallyNewsBot/1.0)'
        })
        resp.raise_for_status()
    except Exception as e:
        print(f"  Feed fetch error: {e}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"  Feed parse error: {e}")
        return []

    MEDIA_NS = 'http://search.yahoo.com/mrss/'

    def local(el):
        return el.tag.split('}', 1)[-1] if '}' in el.tag else el.tag

    def find_local(parent, name):
        return next((c for c in parent if local(c) == name), None)

    def findall_local(parent, name):
        return [c for c in parent if local(c) == name]

    def elem_text(parent, *path):
        node = parent
        for step in path:
            node = find_local(node, step)
            if node is None:
                return ''
        return (node.text or '').strip()

    def parse_entry(item, is_atom):
        entry = {}
        if is_atom:
            entry['title'] = elem_text(item, 'title')
            for link_el in findall_local(item, 'link'):
                href = link_el.get('href', '')
                if href:
                    entry.setdefault('link', href)
                    if link_el.get('rel', 'alternate') == 'alternate':
                        entry['link'] = href
                        break
            summary_el = find_local(item, 'summary')
            if summary_el is None:
                summary_el = find_local(item, 'content')
            entry['summary'] = (summary_el.text or '').strip() if summary_el is not None else ''
            pub_el = find_local(item, 'published')
            if pub_el is None:
                pub_el = find_local(item, 'updated')
            entry['published_parsed'] = (pub_el.text or '').strip() if pub_el is not None else None
        else:
            entry['title'] = elem_text(item, 'title')
            link_el = find_local(item, 'link')
            entry['link'] = (link_el.text or '').strip() if link_el is not None else ''
            if not entry['link']:
                guid_el = find_local(item, 'guid')
                if guid_el is not None:
                    val = (guid_el.text or '').strip()
                    if val.startswith('http'):
                        entry['link'] = val
            desc_el = find_local(item, 'description')
            entry['summary'] = (desc_el.text or '').strip() if desc_el is not None else ''
            pub_el = find_local(item, 'pubDate')
            if pub_el is None:
                pub_el = find_local(item, 'date')
            entry['published_parsed'] = (pub_el.text or '').strip() if pub_el is not None else None

        entry.setdefault('link', '')

        media_content = [{'url': el.get('url')} for el in item
                         if el.tag == f'{{{MEDIA_NS}}}content' and el.get('url')]
        if media_content:
            entry['media_content'] = media_content

        media_thumbnail = [{'url': el.get('url')} for el in item
                           if el.tag == f'{{{MEDIA_NS}}}thumbnail' and el.get('url')]
        if media_thumbnail:
            entry['media_thumbnail'] = media_thumbnail

        enclosures = [{'href': el.get('url', ''), 'type': el.get('type', '')}
                      for el in item if local(el) == 'enclosure' and el.get('url')]
        if enclosures:
            entry['enclosures'] = enclosures

        return entry

    root_local = local(root)
    if root_local == 'feed':
        return [parse_entry(e, is_atom=True) for e in findall_local(root, 'entry')]
    else:
        channel = find_local(root, 'channel')
        parent = channel if channel is not None else root
        return [parse_entry(item, is_atom=False) for item in findall_local(parent, 'item')]


# ═══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def is_recent(article_date):
    """Check if article is within last 48 hours"""
    if not article_date:
        return False

    try:
        pub_date = None
        if isinstance(article_date, str):
            # Try RFC 2822 (handles GMT, +0000, etc.)
            try:
                pub_date = parsedate_to_datetime(article_date)
            except Exception:
                pass
            # Try ISO 8601 variants
            if pub_date is None:
                for fmt in ['%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d']:
                    try:
                        pub_date = datetime.strptime(article_date, fmt)
                        break
                    except Exception:
                        continue
            if pub_date is None:
                return False
        else:
            pub_date = datetime(*article_date[:6])

        if pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=datetime.now().astimezone().tzinfo)

        cutoff = datetime.now(pub_date.tzinfo) - timedelta(hours=48)
        return pub_date > cutoff
    except Exception:
        return False

def call_ai(prompt, timeout=15):
    """Call OpenRouter API with multi-model fallback"""
    if not OPENROUTER_API_KEY:
        print("ERROR: OPENROUTER_API_KEY not set")
        return None

    for model in AI_MODELS:
        try:
            response = requests.post(
                'https://openrouter.ai/api/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': model,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'max_tokens': 100
                },
                timeout=timeout
            )

            if response.status_code == 200:
                result = response.json()['choices'][0]['message']['content'].strip()
                print(f"✓ Model {model} succeeded")
                return result
            else:
                print(f"✗ Model {model} failed: {response.status_code}")
                continue

        except Exception as e:
            print(f"✗ Model {model} error: {str(e)}")
            continue

    print("ERROR: All AI models failed")
    return None

def is_positive_news(title, summary):
    """Use AI to determine if article is genuinely positive news"""
    prompt = f"""Is this article about POSITIVE news (progress, achievements, solutions, help, innovation, recovery, cooperation)? Positive news is not controversial, and is actively showing progress or improvement. The article may be in German or English.

Examples of positive news stories (German/English):
- Durchbruch bei Solarenergie: Neue Zellen erreichen Rekordwirkungsgrad
- Neue U-Bahn-Linie in Hamburg eröffnet – kürzere Fahrzeiten für 50.000 Pendler
- Friedensabkommen zwischen zwei Ländern unterzeichnet
- A Single Infusion Could Suppress H.I.V. for Years, Study Suggests
- Innovation abounds in device charging
- Sharp drop in 'forever chemicals' in seabird eggs hailed as win for regulation
- Macron announces €23 billion of investment at Africa summit

Examples of negative news stories (German/English):
- Anschlag in Berlin: Mehrere Verletzte
- Hochwasser verwüstet Teile Süddeutschlands
- Kennedy Is Driving a Vast Inquiry Into Vaccines, Despite His Public Silence
- Emissions rise by 10% over last year, according to new data
- Man Charged With Assassination Attempt at Press Gala Pleads Not Guilty

Title: {title}
Summary: {summary}

Rules:
- YES only if it's genuinely positive/uplifting
- NO if it's neutral, negative, explanatory, or just informational
- NO if it's about problems, conflicts, crises, or disasters
- NO if it's an explainer or educational content
- NO if it's about controversy or debate

Answer ONLY: YES or NO"""

    result = call_ai(prompt)
    return result and 'YES' in result.upper()

def is_duplicate_topic(new_title, new_summary, recent_articles):
    """Check if this article is about the same topic as recent articles"""
    # Compare with last 20 articles to detect duplicate topics
    for article in recent_articles[:20]:
        prompt = f"""Are these two articles about the SAME topic/event/story? The articles may be in German or English.

Article 1:
Title: {new_title}
Summary: {new_summary}

Article 2:
Title: {article['title']}
Summary: {article.get('summary', article.get('first_paragraph', ''))[:300]}

Rules:
- YES if they're about the same specific event, announcement, or story
- YES if one is a follow-up to the other
- NO if they're just in the same general category
- NO if they're about different aspects of a broader topic

Examples of SAME topic:
- "Nvidia investiert 40 Mrd. in KI" vs "Nvidia setzt auf KI-Strategie" → YES (same investment)
- "Hamburg wählt neuen Bürgermeister" vs "Peter Tschentscher wiedergewählt" → YES (same event)

Examples of DIFFERENT topics:
- "NASA Mars-Rover" vs "SpaceX startet Satellit" → NO (different space stories)
- "Berliner Mietstopp" vs "Wohnungspolitik in München" → NO (different cities)

Answer ONLY: YES or NO"""

        result = call_ai(prompt, timeout=10)
        if result and 'YES' in result.upper():
            print(f"    ✗ Duplicate topic of: {article['title'][:60]}...")
            return True

    return False

def categorize_article(title, summary):
    """Determine article category using AI"""
    prompt = f"""Categorize this article into ONE category. The article may be in German or English.

Title: {title}
Summary: {summary}

Categories:
- climate (Umwelt, Nachhaltigkeit, erneuerbare Energien, Klimaschutz, Emissionen / environment, sustainability, renewable energy)
- transportation (Verkehr, Infrastruktur, Mobilität, Bahn, U-Bahn, Straßen / transit, infrastructure, mobility, trains)
- ai (Technologie, Wissenschaft, Forschung, Innovation, Weltraum, KI / technology, science, research, AI, space)
- business (Wirtschaft, Finanzen, Unternehmen, Startups, Handel / economy, finance, companies, startups, trade)
- politics (Politik, Gesetze, Wahlen, Demokratie, Parlament / government, policy, legislation, elections, democracy)
- entertainment (Film, Musik, Sport, Spiele, TV, Kultur / film, music, celebrity, sports, games, TV)
- world (Internationale Nachrichten, Diplomatie, globale Ereignisse / international news, diplomacy, global affairs)
- religion (Glaube, Kirche, spirituelle Themen / faith, spirituality, churches, religious leaders)
- arts (Kultur, Literatur, Bücher, Museen, Theater / culture, literature, books, museums, theater)

Answer with ONLY the category name (one word)."""

    result = call_ai(prompt, timeout=10)

    if result:
        category = result.strip().lower()
        if category in VALID_CATEGORIES:
            return category

    return 'world'

def extract_first_paragraph(url):
    """Extract first paragraph from article"""
    try:
        response = requests.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; RallyNewsBot/1.0)'
        })
        soup = BeautifulSoup(response.text, 'html.parser')

        # Try common paragraph selectors
        for selector in ['article p', '.article-body p', '.story-body p', 'p']:
            paragraphs = soup.select(selector)
            for p in paragraphs:
                text = p.get_text().strip()
                if len(text) > 100:  # Substantial paragraph
                    return text[:500]

        return None
    except:
        return None

def get_article_image(entry, used_images):
    """Extract unique image URL from article"""
    # Try media content
    if entry.get('media_content'):
        img = entry['media_content'][0].get('url')
        if img and img not in used_images:
            return img

    # Try media thumbnail
    if entry.get('media_thumbnail'):
        img = entry['media_thumbnail'][0].get('url')
        if img and img not in used_images:
            return img

    # Try enclosures
    if entry.get('enclosures'):
        for enc in entry['enclosures']:
            if 'image' in enc.get('type', ''):
                img = enc.get('href')
                if img and img not in used_images:
                    return img

    # Fallback: fetch from page
    try:
        article_url = entry.get('link', '')
        if not article_url:
            return None

        response = requests.get(article_url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; RallyNewsBot/1.0)'
        })
        soup = BeautifulSoup(response.text, 'html.parser')

        # Try og:image
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            img = og_image['content']
            if img not in used_images:
                return img

        # Try first img tag
        img_tag = soup.find('img', src=True)
        if img_tag:
            img = urljoin(article_url, img_tag['src'])
            if img not in used_images:
                return img
    except:
        pass

    return None

# ═══════════════════════════════════════════════════════════════
# MAIN SCRAPER
# ═══════════════════════════════════════════════════════════════

def scrape_news():
    """Main scraping function"""
    print("═" * 60)
    print("RALLY NEWS SCRAPER (German Edition) - Starting")
    print("═" * 60)

    SCRAPE_TIMEOUT = 45 * 60   # 45 minutes max per run
    MIN_NEW_ARTICLES = 6        # target per run
    BATCH_SIZE = 10             # feed entries examined per pass

    start_time = time.time()

    # Load existing articles
    existing_articles = []
    used_images = set()

    try:
        with open('news.json', 'r') as f:
            existing_articles = json.load(f)
            for article in existing_articles:
                article.pop('rallying_cry', None)
            used_images = {a.get('image_url') for a in existing_articles if a.get('image_url')}
            print(f"Loaded {len(existing_articles)} existing articles")
    except FileNotFoundError:
        print("No existing articles found")

    new_articles = []
    checked_urls = set()  # URLs already evaluated this run (across all passes)
    pass_num = 0

    while True:
        elapsed = time.time() - start_time

        if elapsed >= SCRAPE_TIMEOUT:
            print(f"\nTimeout reached after {pass_num} passes ({elapsed/60:.1f} min)")
            break

        if len(new_articles) >= MIN_NEW_ARTICLES:
            print(f"\nTarget reached: {len(new_articles)} new articles found")
            break

        start_idx = pass_num * BATCH_SIZE
        end_idx = start_idx + BATCH_SIZE

        print(f"\n{'─' * 60}")
        print(f"Pass {pass_num + 1}: checking feed entries {start_idx + 1}–{end_idx}")
        print(f"Articles found so far: {len(new_articles)}/{MIN_NEW_ARTICLES}")
        print(f"{'─' * 60}")

        new_candidates_this_pass = 0

        for source_name, feed_url in RSS_FEEDS.items():
            if source_name not in WHITELISTED_SOURCES:
                continue

            if time.time() - start_time >= SCRAPE_TIMEOUT:
                break

            print(f"\nScraping: {source_name}")

            try:
                entries = parse_feed(feed_url)

                for entry in entries[start_idx:end_idx]:
                    url = entry.get('link', '').strip()
                    if not url or url in checked_urls:
                        continue

                    checked_urls.add(url)
                    new_candidates_this_pass += 1

                    pub_date = entry.get('published_parsed')
                    if not is_recent(pub_date):
                        continue

                    title = entry.get('title', '').strip()
                    summary = entry.get('summary', entry.get('description', '')).strip()

                    if not all([title, url]):
                        continue

                    if any(a['url'] == url for a in existing_articles):
                        continue

                    print(f"  Checking: {title[:60]}...")
                    if not is_positive_news(title, summary):
                        print(f"    ✗ Not positive news")
                        continue

                    print(f"    ✓ Positive news!")

                    combined_articles = new_articles + existing_articles
                    if is_duplicate_topic(title, summary, combined_articles):
                        continue

                    print(f"    ✓ Unique topic!")

                    image_url = get_article_image(entry, used_images)
                    if not image_url:
                        print(f"    ✗ No unique image found")
                        continue

                    used_images.add(image_url)

                    first_paragraph = extract_first_paragraph(url)
                    if not first_paragraph:
                        first_paragraph = summary[:500]

                    category = categorize_article(title, summary)
                    print(f"    ✓ Categorized as: {category}")

                    article = {
                        'title': title,
                        'source': source_name,
                        'url': url,
                        'first_paragraph': first_paragraph,
                        'summary': summary[:300] if summary else first_paragraph[:300],
                        'image_url': image_url,
                        'timestamp': datetime.now().isoformat() + 'Z',
                        'category': category
                    }

                    new_articles.append(article)
                    print(f"    ✓ Added ({len(new_articles)}/{MIN_NEW_ARTICLES})")

                    time.sleep(2)

            except Exception as e:
                print(f"  ✗ Error scraping {source_name}: {str(e)}")
                continue

        pass_num += 1

        # No new URLs found anywhere — all feeds exhausted at this depth
        if new_candidates_this_pass == 0:
            print(f"\nNo new entries found in pass {pass_num}. Feeds exhausted.")
            break

    # Merge and deduplicate
    all_articles = new_articles + existing_articles

    seen_urls = set()
    unique_articles = []
    for article in all_articles:
        if article['url'] not in seen_urls:
            seen_urls.add(article['url'])
            unique_articles.append(article)

    unique_articles.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

    # All existing articles are preserved; the 48-hour filter only applies to
    # new RSS candidates above, so nothing already in news.json is ever dropped.
    final_articles = unique_articles

    with open('news.json', 'w') as f:
        json.dump(final_articles, f, indent=2, ensure_ascii=False)

    print("\n" + "═" * 60)
    print(f"COMPLETE: {len(new_articles)} new articles added")
    print(f"Total articles: {len(final_articles)}")
    print("═" * 60)

if __name__ == '__main__':
    scrape_news()
