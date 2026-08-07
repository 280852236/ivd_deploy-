package matcher

import (
    "ivd_analyzer_go/db"
    "strconv"
    "strings"
)

type AnalysisItem struct {
    Type              string                 `json:"type"`
    Keywords          []string               `json:"keywords,omitempty"`
    Advice            string                 `json:"advice,omitempty"`
    Source            string                 `json:"source,omitempty"`
    OriginalText      string                 `json:"original_text"`
    EventTime         string                 `json:"event_time"`
    EventDate         string                 `json:"event_date"`
    MotorMatch        *MotorMatch            `json:"motor_match,omitempty"`
    MatchedConditions []string               `json:"matched_conditions,omitempty"`
    MatchedCount      int                    `json:"matched_count,omitempty"`
}

func AnalyzeText(text, series, model string, skipMotorStatus bool) []AnalysisItem {
    var results []AnalysisItem

    if !skipMotorStatus {
        motorMatches := FindMotorStatusMatches(text, model)
        for _, m := range motorMatches {
            item := AnalysisItem{
                Type:         "motor_status_match",
                Keywords:     m.Keywords,
                Advice:       m.Advice,
                Source:       m.Source,
                OriginalText: m.OriginalText,
                EventTime:    m.EventTime,
                EventDate:    m.EventDate,
                MotorMatch:   &m,
            }
            results = append(results, item)
        }
    }

    lines := strings.Split(text, "\n")
    matchedLines := make(map[string]bool)
    for _, line := range lines {
        line = strings.TrimSpace(line)
        if line == "" {
            continue
        }
        asp := CheckAspirationAnomaly(line)
        if asp.Matched {
            keyword := "样本空吸"
            if asp.Type == "reagent" {
                keyword = "试剂空吸"
            }
			adviceText := "检测到 " + strconv.Itoa(len(asp.Conditions)) + " 个异常条件：" + strings.Join(asp.Conditions, "、") + "。建议检查样本/试剂供应情况和管路连接。"
			eventTime := extractNearestTimestamp(line, 0)
            item := AnalysisItem{
                Type:         "keyword_match",
                Keywords:     []string{keyword},
                Advice:       adviceText,
                Source:       "异常条件检测",
                OriginalText: line,
                EventTime:    eventTime,
                EventDate:    normalizeEventDate(eventTime),
                MatchedConditions: asp.Conditions,
                MatchedCount: len(asp.Conditions),
            }
            results = append(results, item)
            matchedLines[line] = true
        }
    }

    rules, _ := db.GetRules(series, model)
    textLower := strings.ToLower(text)
    for _, rule := range rules {
        keywords := strings.Split(rule.Keywords, ",")
        for _, kw := range keywords {
            kw = strings.TrimSpace(kw)
            if kw == "" {
                continue
            }
            kwLower := strings.ToLower(kw)
            startIdx := 0
            for {
                idx := strings.Index(textLower[startIdx:], kwLower)
                if idx == -1 {
                    break
                }
                idx += startIdx
                orig := extractLineContext(text, idx)
                if matchedLines[orig] {
                    startIdx = idx + 1
                    continue
                }
                eventTime := extractNearestTimestamp(text, idx)
                source := "手动规则"
                if rule.Source == "pdf" {
                    source = "PDF知识库"
                }
                item := AnalysisItem{
                    Type:         "keyword_match",
                    Keywords:     []string{kw},
                    Advice:       rule.Advice,
                    Source:       source,
                    OriginalText: orig,
                    EventTime:    eventTime,
                    EventDate:    normalizeEventDate(eventTime),
                }
                results = append(results, item)
                matchedLines[orig] = true
                startIdx = idx + 1
            }
        }
    }
    return results
}

func extractLineContext(text string, index int) string {
    start := strings.LastIndex(text[:index], "\n") + 1
    end := strings.Index(text[index:], "\n")
    if end == -1 {
        end = len(text)
    } else {
        end += index
    }
    return strings.TrimSpace(text[start:end])
}

func extractNearestTimestamp(text string, index int) string {
	if len(text) == 0 {
		return ""
	}
	window := 250
	start := index - window
	if start < 0 {
		start = 0
	}
	end := index + window
	if end > len(text) {
		end = len(text)
	}
	context := text[start:end]
	for _, pat := range timestampPatterns {
		allMatches := pat.FindAllStringIndex(context, -1)
		if len(allMatches) > 0 {
			last := allMatches[len(allMatches)-1]
			return context[last[0]:last[1]]
		}
	}
	return ""
}

func normalizeEventDate(eventTime string) string {
    if eventTime == "" {
        return "未识别日期"
    }
    parts := strings.Split(eventTime, " ")
    if len(parts) > 0 {
        date := parts[0]
        return strings.ReplaceAll(date, "/", "-")
    }
    return "未识别日期"
}
