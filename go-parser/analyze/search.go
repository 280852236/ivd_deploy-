package analyze

import (
	"regexp"
	"strings"
)

type SearchMatch struct {
	LineNum int    `json:"line_num"`
	Col     int    `json:"col"`
	Text    string `json:"text"`
	Match   string `json:"match"`
}

type SearchResult struct {
	Matches []SearchMatch `json:"matches"`
	Total   int           `json:"total"`
	Truncated bool        `json:"truncated"`
}

var maxSearchMatches = 5000

func SearchText(content, pattern string, isRegex, caseSensitive, wholeWord bool) *SearchResult {
	if pattern == "" || content == "" {
		return &SearchResult{Matches: []SearchMatch{}, Total: 0}
	}

	var re *regexp.Regexp
	var err error

	flags := ""
	if !caseSensitive {
		flags = "(?i)"
	}

	pat := pattern
	if !isRegex {
		pat = regexp.QuoteMeta(pattern)
	}
	if wholeWord {
		pat = `\b` + pat + `\b`
	}

	re, err = regexp.Compile(flags + pat)
	if err != nil {
		return &SearchResult{Matches: []SearchMatch{}, Total: 0}
	}

	lines := strings.Split(content, "\n")
	matches := make([]SearchMatch, 0, 100)
	total := 0

	for lineIdx, line := range lines {
		if len(line) == 0 {
			continue
		}
		locs := re.FindAllStringIndex(line, -1)
		for _, loc := range locs {
			total++
			if len(matches) < maxSearchMatches {
				text := line
				if len(text) > 300 {
					text = text[:300]
				}
				matches = append(matches, SearchMatch{
					LineNum: lineIdx + 1,
					Col:     loc[0],
					Text:    text,
					Match:   line[loc[0]:loc[1]],
				})
			}
		}
		if total > maxSearchMatches*2 {
			break
		}
	}

	return &SearchResult{
		Matches:   matches,
		Total:     total,
		Truncated: total > maxSearchMatches,
	}
}
