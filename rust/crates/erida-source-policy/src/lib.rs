const PROMOTION_PREFIXES: &[&str] = &[
    "最新小说",
    "最新小說",
    "本书首发",
    "本書首發",
    "记住我们",
    "記住我們",
    "记住本站",
    "記住本站",
    "记住全网最快",
    "記住全網最快",
    "самыесвежиеглав",
    "самоеновоечтение",
    "перваяпубликациякниги",
    "запомнитенас",
    "свежиеглавы",
    "latestchapters",
    "firstpublished",
];

const PROMOTION_FRAGMENTS: &[&str] = &[
    "首发",
    "首發",
    "最新章节",
    "最新章節",
    "全文阅读",
    "全文閱讀",
    "无错章节",
    "無錯章節",
    "无乱序",
    "無亂序",
    "更新不易",
    "记得分享",
    "記得分享",
    "小说站",
    "小説站",
    "最快更新",
    "толькона нашемсайте",
    "тольконашемсайте",
    "толькоздесь",
    "безпропусковиошибок",
    "верныеглавы",
    "onlyonoursite",
    "withoutmissingchapters",
];

const DOMAIN_PROMOTION_FRAGMENTS: &[&str] = &[
    "希望读者记一下我们域名",
    "希望讀者記一下我們域名",
    "看书网藏书全",
    "看書網藏書全",
    "全手打无错站",
    "全手打無錯站",
    "看书网伴你读",
    "看書網伴你讀",
    "超靠谱",
    "超靠譜",
    "超顺畅",
    "超順暢",
    "авторпроситчитателейзапомнитьнашдомен",
    "追书就上",
    "追書就上",
    "观看最快的章节更新",
    "觀看最快的章節更新",
    "看书网书库多",
    "看書網書庫多",
    "看书网藏书广",
    "看書網藏書廣",
    "任你选网站",
    "任你選網站",
    "任你读网站",
    "任你讀網站",
    "超赞网站",
    "超讚網站",
];

//! Partial Rust port of ReadEra's *standalone text* source-unit policy.
//! This is NOT DOM classification, BeautifulSoup serialization or an
//! authority for calculating source hashes. Integration into writers is
//! explicitly forbidden until full end-to-end source-unit parity exists.
use regex::Regex;
use std::sync::OnceLock;
use unicode_normalization::UnicodeNormalization;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TextDecision {
    pub kind: &'static str,
    pub reason: &'static str,
}

fn pattern(cell: &'static OnceLock<Regex>, regex: &'static str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(regex).expect("static source-policy regular expression"))
}
fn domain_pattern() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    pattern(&RE, r"(?i)101(?:kanshu|kks|read|看書網|看书网)(?:\.[a-z]{2,})?")
}
fn chapter_end_pattern() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    pattern(&RE, r"^[（(]?本章完[)）]?$")
}
fn next_chapter_pattern() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    pattern(&RE, r"^[<\[【（(]?\s*(?:下一章|下章|下一節|下一节|下一回|下一頁|下一页|下頁|下页|next\s+chapter)(?:\s+.*|[:：>»›】\]）)].*)?$")
}
fn tail_hint_pattern() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    pattern(&RE, r"^(?:溫馨提示|温馨提示)\s*[:：]?$")
}

fn zero_width(character: char) -> bool {
    matches!(character, '\u{200b}'..='\u{200f}' | '\u{2060}' | '\u{feff}')
}
fn decorative(character: char) -> bool {
    matches!(
        character, '⊥' | '⊤' | '◆' | '◇' | '■' | '□' | '●' | '○'
        | '·' | '•' | '⋅' | '丨' | '|' | '¦' | '＿' | '_' | '~'
        | '～' | '*' | '=' | '+'
    )
}
fn allowed_alnum(character: char) -> bool {
    matches!(character, '0'..='9' | 'a'..='z' | 'а'..='я' | 'ё'
        | '\u{3400}'..='\u{4dbf}' | '\u{4e00}'..='\u{9fff}'
        | '\u{f900}'..='\u{faff}' | '\u{20000}'..='\u{2fa1f}')
}

fn normalize(text: &str) -> String {
    let decoded = html_escape::decode_html_entities(text);
    let normalized: String = decoded
        .nfkc()
        .filter(|ch| !zero_width(*ch))
        .flat_map(char::to_lowercase)
        .collect();
    normalized.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn compact_views(text: &str) -> (String, String, String) {
    let normalized = normalize(text);
    let compact = normalized
        .chars()
        .filter(|ch| !decorative(*ch) && !ch.is_whitespace())
        .collect::<String>();
    let alnum = compact.chars().filter(|ch| allowed_alnum(*ch)).collect();
    (normalized, compact, alnum)
}

fn domain_standalone(compact: &str) -> bool {
    let without_domain = domain_pattern().replace_all(compact, "");
    without_domain.chars().all(|ch| {
        matches!(ch,
            '[' | ']' | '【' | '】' | '(' | ')' | '（' | '）'
            | '<' | '>' | '《' | '》' | ':' | '：' | '/' | '\\'
            | '.' | '!' | '！' | '?' | '？' | ',' | '，' | ';'
            | '；' | '\'' | '"' | '“' | '”' | '‘' | '’' | '—'
            | '–' | '−' | '-'
        )
    })
}
fn starts_promotion(normalized: &str, alnum: &str) -> bool {
    let stripped = normalized.trim_start_matches(&['[','【','(','（','<','《','"','“','\''][..]);
    let stripped_alnum: String = stripped.chars().filter(|ch| allowed_alnum(*ch)).collect();
    PROMOTION_PREFIXES
        .iter()
        .any(|prefix| stripped_alnum.starts_with(prefix) || alnum.starts_with(prefix))
}

pub fn is_source_boilerplate_text(text: &str) -> bool {
    let (normalized, compact, alnum) = compact_views(text);
    if compact.is_empty() {
        return false;
    }
    if chapter_end_pattern().is_match(&compact) {
        return true;
    }
    let has_domain = domain_pattern().is_match(&compact);
    let promotion = starts_promotion(&normalized, &alnum)
        || PROMOTION_FRAGMENTS.iter().any(|fragment| {
            alnum.contains(&fragment.replace(' ', ""))
        });
    let domain_promotion = DOMAIN_PROMOTION_FRAGMENTS.iter().any(|fragment| {
        alnum.contains(&fragment.replace(' ', ""))
    });
    if has_domain && (domain_standalone(&compact) || promotion || domain_promotion) {
        return true;
    }
    promotion && normalized.chars().count() <= 260
}

/// In Python, this is only removed when it appears at the LAST visible
/// node. The standalone decision below deliberately does not remove it.
pub fn is_source_tail_boilerplate_text(text: &str) -> bool {
    tail_hint_pattern().is_match(normalize(text).trim())
}

/// Equivalent to Python source_units.classify_source_text, not its DOM policy.
/// Casefold-unstable unicode scripts are pending independent validation.
pub fn classify_source_text(text: &str) -> TextDecision {
    if is_source_boilerplate_text(text) {
        return TextDecision {
            kind: "SOURCE_BOILERPLATE",
            reason: "scraper_or_site_boilerplate",
        };
    }
    let normalized = normalize(text);
    if next_chapter_pattern().is_match(&normalized) {
        TextDecision {
            kind: "NEXT_CHAPTER_BOUNDARY",
            reason: "explicit_next_chapter_marker",
        }
    } else {
        TextDecision {
            kind: "CONTENT",
            reason: "translatable_content",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_standalone_text_without_deleting_narrative() {
        assert_eq!(classify_source_text("你好，世界").kind, "CONTENT");
        assert_eq!(classify_source_text("101kanshu appeared in a character's speech").kind, "CONTENT");
        assert_eq!(classify_source_text("本章完").kind, "SOURCE_BOILERPLATE");
        assert_eq!(classify_source_text("101kanshu.com").kind, "SOURCE_BOILERPLATE");
        assert_eq!(classify_source_text("记住本站").kind, "SOURCE_BOILERPLATE");
        assert_eq!(classify_source_text("next chapter").kind, "NEXT_CHAPTER_BOUNDARY");
        assert_eq!(classify_source_text("下一章：我").kind, "NEXT_CHAPTER_BOUNDARY");
    }

    #[test]
    fn tail_hint_needs_position_context_before_deletion() {
        assert!(is_source_tail_boilerplate_text("溫馨提示："));
        assert!(!is_source_boilerplate_text("溫馨提示："));
        assert_eq!(classify_source_text("溫馨提示：").kind, "CONTENT");
    }

    #[test]
    fn whitespace_and_nfkc_are_applied_to_policy_only() {
        assert_eq!(classify_source_text("  本章完  ").kind, "SOURCE_BOILERPLATE");
        assert_eq!(classify_source_text("Ｎｅｘｔ　Ｃｈａｐｔｅｒ").kind, "NEXT_CHAPTER_BOUNDARY");
        assert_eq!(classify_source_text("仅是普通章节对话").kind, "CONTENT");
    }
}
