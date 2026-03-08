import pandas as pd
import time
import re
import os
import unicodedata
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

class WattpadScraperV5:
    """
    Wattpad scraper v5.0
    KEY FIX: Wattpad splits chapters into multiple pages (/page/1, /page/2, ...).
             The scraper now visits ALL pages of a chapter and combines the text.
    """

    def __init__(self, headless=False, scrape_chapter_stats=True, extract_chapter_text=False):
        self.headless = headless
        self.should_scrape_stats = scrape_chapter_stats
        self.should_extract_text = extract_chapter_text
        self.driver = None
        self._init_driver()

    def _init_driver(self):
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass

        options = Options()
        if self.headless:
            options.add_argument('--headless=new')

        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument("window-size=1920,1080")
        options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=options)

        self.driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        self.driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def normalize_text(self, text):
        if not text:
            return ""
        normalized = unicodedata.normalize('NFKD', text)
        ascii_text = normalized.encode('ascii', 'ignore').decode('ascii').strip()
        return re.sub(r'\s+', ' ', ascii_text)

    def safe_filename(self, text, max_length=60):
        """Converts a story title into a safe filename/folder string."""
        safe = re.sub(r'[<>:"/\\|?*]', '', text)
        safe = safe.strip().replace(' ', '_')
        safe = re.sub(r'_+', '_', safe)
        return safe[:max_length]

    def _parse_volume_string(self, text):
        try:
            text = text.upper().replace(',', '').replace(' ', '')
            multiplier = 1
            if 'K' in text:
                multiplier = 1_000;    text = text.replace('K', '')
            elif 'M' in text:
                multiplier = 1_000_000; text = text.replace('M', '')
            elif 'B' in text:
                multiplier = 1_000_000_000; text = text.replace('B', '')
            return float(text) * multiplier
        except:
            return 0.0

    # ------------------------------------------------------------------
    # Page loading helpers
    # ------------------------------------------------------------------

    def _load_page_content(self):
        """Scrolls the page to trigger lazy-loaded elements."""
        print("    ...Loading full page content...")
        total_height = self.driver.execute_script("return document.body.scrollHeight")
        for i in range(1, 6):
            self.driver.execute_script(f"window.scrollTo(0, {total_height * (i/5)});")
            time.sleep(0.8)

        try:
            toc = self.driver.find_element(By.CLASS_NAME, "story-parts")
            self.driver.execute_script(
                "arguments[0].scrollIntoView({behavior:'smooth',block:'center'});", toc
            )
            time.sleep(2)
        except:
            self.driver.execute_script(f"window.scrollTo(0, {total_height * 0.4});")
            time.sleep(2)

        try:
            buttons = self.driver.find_elements(
                By.XPATH, "//button[contains(text(),'Show more') or contains(@class,'more')]"
            )
            for btn in buttons:
                if btn.is_displayed():
                    self.driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1)
        except:
            pass

    def _get_total_pages(self, soup, base_chapter_url):
        """
        Detects how many pages a chapter has.
        Looks for page-navigation links like /page/3 in the HTML.
        """
        # Strip any existing /page/N suffix from the base URL
        base_url = re.sub(r'/page/\d+$', '', base_chapter_url.rstrip('/'))

        page_nums = set()
        for a in soup.find_all('a', href=True):
            href = a['href']
            m = re.search(r'/page/(\d+)', href)
            if m:
                page_nums.add(int(m.group(1)))

        # Also check <link rel="next"> tags
        for link in soup.find_all('link', rel=True):
            if 'next' in link.get('rel', []):
                m = re.search(r'/page/(\d+)', link.get('href', ''))
                if m:
                    page_nums.add(int(m.group(1)))

        return max(page_nums) if page_nums else 1

    # ------------------------------------------------------------------
    # Text extraction (single page)
    # ------------------------------------------------------------------

    def _extract_text_from_soup(self, soup):
        """Extract story paragraphs from a single page's soup."""
        text_content = []

        paragraphs = soup.find_all('p', attrs={'data-p-id': True})
        if paragraphs:
            for p in paragraphs:
                # Remove inline comment/UI widgets
                for ui in p.find_all(
                    ['div', 'button'],
                    class_=re.compile(r'component-wrapper|comment-marker')
                ):
                    ui.decompose()
                para_text = p.get_text(strip=True)
                if para_text and para_text != '<br>':
                    text_content.append(para_text)

        # Fallback: pre > p
        if not text_content:
            pre_tag = soup.find('pre', class_=re.compile(r'chapter-text|story-text'))
            if pre_tag:
                for ui in pre_tag.find_all(
                    ['div', 'button'],
                    class_=re.compile(r'component-wrapper|comment-marker')
                ):
                    ui.decompose()
                for p in pre_tag.find_all('p'):
                    t = p.get_text(strip=True)
                    if t:
                        text_content.append(t)

        return text_content

    # ------------------------------------------------------------------
    # *** CORE FIX: Multi-page chapter text extraction ***
    # ------------------------------------------------------------------

    def extract_full_chapter_text(self, chapter_base_url):
        """
        Visits every page of a chapter (/page/1, /page/2, ...) and returns
        the combined text as a single string.

        WHY THIS IS NEEDED:
        Wattpad splits long chapters into multiple pages. The base chapter URL
        only shows page 1. Pages 2, 3, 4 ... must be fetched separately.
        """
        all_paragraphs = []

        # ---- Page 1 (already loaded by caller, but we load it fresh here) ----
        base_url = re.sub(r'/page/\d+$', '', chapter_base_url.rstrip('/'))
        page1_url = f"{base_url}/page/1"

        print(f"      → Fetching page 1 ...")
        self.driver.get(page1_url)
        time.sleep(3)

        soup = BeautifulSoup(self.driver.page_source, 'html.parser')
        all_paragraphs.extend(self._extract_text_from_soup(soup))

        # ---- Detect total pages ----
        total_pages = self._get_total_pages(soup, base_url)
        print(f"      → Chapter has {total_pages} page(s).")

        # ---- Fetch remaining pages ----
        for page_num in range(2, total_pages + 1):
            page_url = f"{base_url}/page/{page_num}"
            print(f"      → Fetching page {page_num} ...")
            self.driver.get(page_url)
            time.sleep(3)

            page_soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            page_paragraphs = self._extract_text_from_soup(page_soup)
            all_paragraphs.extend(page_paragraphs)
            print(f"         ({len(page_paragraphs)} paragraphs found on page {page_num})")

        return '\n\n'.join(all_paragraphs)

    # ------------------------------------------------------------------
    # Stats scraping (with optional full-chapter text)
    # ------------------------------------------------------------------

    def scrape_chapter_stats(self, chapter_url, extract_text=False):
        """
        Visits a chapter page, extracts stats, and optionally the FULL text
        across ALL pages of that chapter.
        """
        try:
            print(f"    Scanning: {chapter_url[:60]}...")
            self.driver.get(chapter_url)
            time.sleep(3)

            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            stats = {"Reads": "N/A", "Votes": "N/A", "Comments": "N/A"}

            # Remove sidebar/recommendation noise
            for noise in soup.find_all(
                ['div', 'section', 'aside'],
                class_=re.compile(r'recommend|sidebar|next-up|story-list|similar|related|footer|you-may-also')
            ):
                noise.decompose()
            for div in soup.find_all('div'):
                text = div.get_text(strip=True)
                if any(p in text.lower() for p in ['you may also like', 'recommended', 'similar stories', 'more stories']):
                    if len(text) < 500:
                        div.decompose()

            # ---- Extract FULL text (all pages) ----
            if extract_text:
                print(f"      Extracting full chapter text (all pages)...")
                stats['Chapter_Text'] = self.extract_full_chapter_text(chapter_url)
                print(f"      Total paragraphs: {stats['Chapter_Text'].count(chr(10)*2) + 1}")

            # ---- Stats: Method 1 — story-stats div ----
            stats_div = soup.find('div', class_='story-stats')
            if stats_div:
                reads_span = stats_div.find('span', class_='reads')
                if reads_span:
                    title_attr = (reads_span.get('title') or
                                  reads_span.get('data-original-title') or
                                  reads_span.get('data-toggle'))
                    if title_attr:
                        m = re.search(r'([\d,]+)\s+Read', title_attr, re.IGNORECASE)
                        if m:
                            stats['Reads'] = m.group(1).replace(',', '')

                votes_span = stats_div.find('span', class_='votes')
                if votes_span:
                    m = re.search(r'([\d,]+)', votes_span.get_text(strip=True))
                    if m:
                        stats['Votes'] = m.group(1).replace(',', '')

                comments_span = stats_div.find('span', class_='comments')
                if comments_span:
                    m = re.search(r'([\d,]+)', comments_span.get_text(strip=True))
                    if m:
                        stats['Comments'] = m.group(1).replace(',', '')

            # ---- Stats: Method 2 — sr-only spans ----
            if stats['Reads'] == "N/A" or stats['Votes'] == "N/A":
                main_area = soup.find('div', id='story-reading') or soup.find('article') or soup
                for span in main_area.find_all('span', class_='sr-only'):
                    text = span.get_text(strip=True)
                    rm = re.search(r'Reads?\s+([\d,]+)', text, re.IGNORECASE)
                    vm = re.search(r'Votes?\s+([\d,]+)', text, re.IGNORECASE)
                    cm = re.search(r'Comments?\s+([\d,]+)', text, re.IGNORECASE)
                    if rm and stats['Reads'] == "N/A":
                        stats['Reads'] = rm.group(1).replace(',', '')
                    if vm and stats['Votes'] == "N/A":
                        stats['Votes'] = vm.group(1).replace(',', '')
                    if cm and stats['Comments'] == "N/A":
                        stats['Comments'] = cm.group(1).replace(',', '')
                    if all(v != "N/A" for v in stats.values()):
                        break

            # ---- Stats: Method 3 — aria-label ----
            if stats['Reads'] == "N/A" or stats['Votes'] == "N/A":
                num_re = r'([\d,]+(?:\.\d+)?\s*[KMB]?)'
                main_content = soup.find('div', id='story-reading') or soup.find('article') or soup
                for el in main_content.find_all(attrs={"aria-label": True}):
                    label = el['aria-label'].lower()
                    vm = re.search(num_re, label, re.IGNORECASE)
                    if vm:
                        num = vm.group(1).replace(' ', '').replace(',', '')
                        if 'read' in label and stats['Reads'] == "N/A":
                            stats['Reads'] = num
                        elif 'vote' in label and stats['Votes'] == "N/A":
                            stats['Votes'] = num
                        elif 'comment' in label and stats['Comments'] == "N/A":
                            stats['Comments'] = num
                    if all(v != "N/A" for v in stats.values()):
                        break

            return stats

        except Exception as e:
            print(f"      [Error: {e}]")
            return {"Reads": "Error", "Votes": "Error", "Comments": "Error"}

    # ------------------------------------------------------------------
    # Local chapter test
    # ------------------------------------------------------------------

    def run_local_chapter_test(self, local_file_path, extract_text=False):
        print(f"\n{'='*70}")
        print(f"TESTING LOCAL CHAPTER: {local_file_path}")
        print(f"{'='*70}")

        abs_path = os.path.abspath(local_file_path)
        if not os.path.exists(abs_path):
            print(f"[ERROR] File not found: {abs_path}")
            return

        file_url = f"file://{abs_path}"
        stats = self.scrape_chapter_stats(file_url, extract_text=extract_text)

        print(f"\n{'='*70}")
        print("EXTRACTION RESULTS")
        print(f"{'='*70}")
        print(f"  Reads:    {stats['Reads']}")
        print(f"  Votes:    {stats['Votes']}")
        print(f"  Comments: {stats['Comments']}")

        if extract_text and 'Chapter_Text' in stats:
            print(f"\n{'='*70}")
            print("CHAPTER TEXT (First 500 chars)")
            print(f"{'='*70}")
            print(stats['Chapter_Text'][:500] + "...")
            print(f"\nTotal characters: {len(stats['Chapter_Text'])}")

        print(f"{'='*70}")

    # ------------------------------------------------------------------
    # Metadata & chapter list parsers
    # ------------------------------------------------------------------

    def parse_metadata_bs4(self, html_content, url):
        soup = BeautifulSoup(html_content, 'html.parser')

        # Title
        title = "Unknown"
        title_tag = soup.find('h1') or soup.select_one('.story-info h1') or soup.select_one('[class*="title"]')
        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            m = re.search(r'/story/\d+-([^?]+)', url)
            if m:
                from urllib.parse import unquote
                title = unquote(m.group(1)).replace('-', ' ')

        # Author
        author = "Unknown"
        for a in soup.find_all('a', href=re.compile(r'^/user/')):
            cls = str(a.get('class', '')).lower()
            if 'avatar' not in cls and 'profile' not in cls:
                t = a.get_text(strip=True)
                if t and len(t) > 1 and t.lower() not in ['wattpad', 'home']:
                    author = t
                    break
        if author == "Unknown":
            pt = soup.find('title')
            if pt:
                parts = pt.get_text(strip=True).split('-')
                if len(parts) >= 3:
                    pa = parts[-2].strip()
                    if pa.lower() != "wattpad":
                        author = pa

        # Description
        desc = ""
        desc_tag = soup.find('pre') or soup.select_one('.description') or soup.select_one('.story-description')
        if desc_tag:
            desc = desc_tag.get_text("\n", strip=True)

        # Stats
        reads = votes = parts_count = "0"
        for span in soup.find_all('span', class_='sr-only'):
            t = span.get_text(strip=True)
            rm = re.search(r'Reads?\s+([\d,]+)', t, re.IGNORECASE)
            vm = re.search(r'Votes?\s+([\d,]+)', t, re.IGNORECASE)
            pm = re.search(r'Parts?\s+(\d+)', t, re.IGNORECASE)
            if rm: reads = rm.group(1).replace(',', '')
            if vm: votes = vm.group(1).replace(',', '')
            if pm: parts_count = pm.group(1)

        if reads == "0" or votes == "0":
            num_re = r'([\d,]+(?:\.\d+)?\s*[KMB]?)'
            for el in soup.find_all(attrs={"aria-label": True}):
                label = el['aria-label'].lower()
                vm = re.search(num_re, label, re.IGNORECASE)
                if vm:
                    num = vm.group(1).replace(' ', '').replace(',', '')
                    if 'read' in label and reads == "0": reads = num
                    elif 'vote' in label and votes == "0": votes = num
                    elif 'part' in label and parts_count == "0": parts_count = num

        # Tags
        tags = []
        for t in soup.find_all('a', class_=re.compile(r'pill__')):
            txt = t.get_text(strip=True)
            if txt:
                tags.append(txt)
        if not tags:
            for t in soup.select('a[href*="/stories/"]'):
                cls = str(t.get('class', ''))
                if 'tag' in cls or 'pill' in cls:
                    tags.append(t.get_text(strip=True))

        return {
            "Story_ID": url.split('/')[-1].split('-')[0] if '/' in url and '-' in url else "Unknown",
            "Title": self.normalize_text(title).title(),
            "Author": self.normalize_text(author),
            "Description": self.normalize_text(desc),
            "Total_Reads": reads,
            "Total_Votes": votes,
            "Total_Parts": parts_count,
            "Tags": " | ".join(tags[:15]),
            "Story_URL": url
        }

    def parse_chapters_bs4(self, html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        chapters = []
        seen_urls = set()

        story_parts = soup.find('ul', attrs={'aria-label': 'story-parts'})
        if story_parts:
            for li in story_parts.find_all('li', recursive=False):
                link = li.find('a', href=True)
                if not link:
                    continue
                href = link['href']
                if '/story/' in href or not re.search(r'/\d+-', href):
                    continue
                title_div = link.find('div', class_='wpYp-')
                title = title_div.get_text(strip=True) if title_div else "Unknown"
                date_div = link.find('div', class_='bSGSB')
                date = date_div.get_text(strip=True) if date_div else "Unknown"
                full_url = href if href.startswith('http') else f"https://www.wattpad.com{href}"
                if full_url not in seen_urls and title:
                    seen_urls.add(full_url)
                    chapters.append({
                        "Order": len(chapters) + 1,
                        "Title": self.normalize_text(title),
                        "URL": full_url,
                        "Published_Date": date
                    })

        if not chapters:
            for link in soup.find_all('a', href=True):
                href = link['href']
                text = link.get_text(strip=True)
                if not re.search(r'/\d+-', href): continue
                if '/story/' in href: continue
                if any(x in href for x in ['/user/', '/list/', '/login', '/search', '/myworks']): continue
                full_url = href if href.startswith('http') else f"https://www.wattpad.com{href}"
                if full_url not in seen_urls and text:
                    date = "Unknown"
                    parent = link.find_parent('li') or link.find_parent('div')
                    if parent:
                        pt = parent.get_text(" ", strip=True)
                        dm = re.search(r'([A-Z][a-z]{2},\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})', pt)
                        if not dm:
                            dm = re.search(r'([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})', pt)
                        if dm:
                            date = dm.group(1)
                    seen_urls.add(full_url)
                    chapters.append({
                        "Order": len(chapters) + 1,
                        "Title": self.normalize_text(text),
                        "URL": full_url,
                        "Published_Date": date
                    })

        return chapters

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    def run(self, url, local_file_path=None):
        try:
            html_source = ""

            if local_file_path:
                print(f"\n{'='*70}")
                print(f"LOADING LOCAL STORY: {local_file_path}")
                print(f"{'='*70}")
                abs_path = os.path.abspath(local_file_path)
                self.driver.get(f"file://{abs_path}")
                time.sleep(5)
                html_source = self.driver.page_source
                print("✓ Local file loaded.")
            else:
                print(f"\n{'='*70}")
                print(f"SCRAPING URL: {url}")
                print(f"{'='*70}")
                self.driver.get(url)
                time.sleep(5)
                self._load_page_content()
                html_source = self.driver.page_source

            print("\nAnalyzing Metadata...")
            story_meta = self.parse_metadata_bs4(html_source, url)

            print("\nAnalyzing Chapters...")
            chapters = self.parse_chapters_bs4(html_source)
            print(f"  Found {len(chapters)} chapters.")

            if len(chapters) > 0:
                story_meta['Total_Parts'] = str(len(chapters))

            print(f"\n{'='*70}")
            print("STORY METADATA")
            print(f"{'='*70}")
            print(f"  Title:       {story_meta['Title']}")
            print(f"  Author:      {story_meta['Author']}")
            print(f"  Reads:       {story_meta['Total_Reads']}")
            print(f"  Votes:       {story_meta['Total_Votes']}")
            print(f"  Parts:       {story_meta['Total_Parts']}")
            print(f"  Description: {story_meta['Description'][:100]}...")
            print(f"  Tags:        {story_meta['Tags'][:80]}...")
            print(f"{'='*70}")

            story_safe_name = self.safe_filename(story_meta['Title']) or "story"

            if self.should_scrape_stats and chapters and not local_file_path:
                print(f"\n{'='*70}")
                print(f"SCRAPING CHAPTER STATS + TEXT (all pages per chapter)")
                print(f"{'='*70}")

                for i, chapter in enumerate(chapters, 1):
                    print(f"\n  [{i}/{len(chapters)}] {chapter['Title'][:50]}")

                    if i > 1 and i % 10 == 0:
                        self.driver.quit()
                        self._init_driver()
                        time.sleep(1)

                    stats = self.scrape_chapter_stats(
                        chapter['URL'], extract_text=self.should_extract_text
                    )
                    chapter.update(stats)
                    print(f"  ✓ Reads:{stats['Reads']}  Votes:{stats['Votes']}  Comments:{stats['Comments']}")

                    if i % 5 == 0:
                        pd.DataFrame(chapters).to_csv(
                            f"{story_safe_name}_chapters_progress.csv", index=False
                        )

            print(f"\n{'='*70}")
            print("SAVING RESULTS")
            print(f"{'='*70}")

            df_story    = pd.DataFrame([story_meta])
            df_chapters = pd.DataFrame(chapters)

            suffix      = "_local" if local_file_path else "_complete"
            story_csv   = f"{story_safe_name}_metadata{suffix}.csv"
            chapter_csv = f"{story_safe_name}_chapters{suffix}.csv"

            df_story.to_csv(story_csv,   index=False, encoding='utf-8-sig')
            df_chapters.to_csv(chapter_csv, index=False, encoding='utf-8-sig')
            print(f"✓ Saved: {story_csv}")
            print(f"✓ Saved: {chapter_csv}")

            if self.should_extract_text and 'Chapter_Text' in df_chapters.columns:
                print(f"\nSaving individual chapter text files...")
                text_dir = story_safe_name          # folder named after the story
                os.makedirs(text_dir, exist_ok=True)

                saved_count = 0
                for idx, row in df_chapters.iterrows():
                    if pd.notna(row.get('Chapter_Text')) and row['Chapter_Text']:
                        title     = row.get('Title', f"Chapter {idx+1}")
                        safe_t    = re.sub(r'[<>:"/\\|?*]', '', title).strip()[:100]
                        filename  = f"{idx+1:02d} - {safe_t}.txt"
                        filepath  = os.path.join(text_dir, filename)
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.write(row['Chapter_Text'])
                        saved_count += 1

                print(f"✓ Saved {saved_count} chapter text files to '{text_dir}/' folder")

            print(f"{'='*70}")
            print("✓ SCRAPING COMPLETE!")

        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()
        finally:
            if self.driver:
                self.driver.quit()


# ======================================================================
# Entry point
# ======================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("WATTPAD SCRAPER V5.0 — Multi-Page Chapter Support")
    print("=" * 70)
    print("1. Scrape Live URL (Metadata + Chapters)")
    print("2. Scrape Live URL (Metadata + Chapters + Stats)")
    print("3. Scrape Live URL (Full: Metadata + Chapters + Stats + TEXT)")
    print("4. Test on LOCAL FILE (Story Overview)")
    print("5. Test on LOCAL FILE (Single Chapter Stats)")
    print("6. Test on LOCAL FILE (Single Chapter Stats + Text)")

    choice = input("\nEnter choice (1-6): ").strip()

    url          = "https://www.wattpad.com/story/353975883-homecoming"
    local_file   = None
    scrape_stats = False
    extract_text = False

    if choice == '4':
        local_file = input("Enter Story .mhtml filename: ").strip().strip('"')
        if os.path.exists(local_file):
            scraper = WattpadScraperV5(headless=False, scrape_chapter_stats=False)
            scraper.run(url, local_file_path=local_file)
        else:
            print("File not found.")

    elif choice == '5':
        local_file = input("Enter Chapter .mhtml filename: ").strip().strip('"')
        if os.path.exists(local_file):
            scraper = WattpadScraperV5(headless=False, scrape_chapter_stats=True)
            scraper.run_local_chapter_test(local_file, extract_text=False)
        else:
            print("File not found.")

    elif choice == '6':
        local_file = input("Enter Chapter .mhtml filename: ").strip().strip('"')
        if os.path.exists(local_file):
            scraper = WattpadScraperV5(headless=False, scrape_chapter_stats=True,
                                       extract_chapter_text=True)
            scraper.run_local_chapter_test(local_file, extract_text=True)
        else:
            print("File not found.")

    else:
        url_input = input("Enter Wattpad story URL (press Enter for default): ").strip()
        if url_input:
            url = url_input

        if choice == '2':
            scrape_stats = True
        elif choice == '3':
            scrape_stats = True
            extract_text = True

        scraper = WattpadScraperV5(headless=False, scrape_chapter_stats=scrape_stats,
                                   extract_chapter_text=extract_text)
        scraper.run(url)