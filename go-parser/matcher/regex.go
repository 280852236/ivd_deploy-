package matcher

import "regexp"

var (
    longHexPattern   = regexp.MustCompile(`([A-F0-9]{12,32})`)
    triplePattern    = regexp.MustCompile(`([0-9A-Fa-f]{2})\s+([0-9A-Fa-f]{2})\s+([0-9A-Fa-f]{2})`)
    timestampPatterns = []*regexp.Regexp{
        regexp.MustCompile(`\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}`),
        regexp.MustCompile(`\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}`),
        regexp.MustCompile(`\d{1,2}:\d{2}:\d{2}`),
        regexp.MustCompile(`\d{1,2}:\d{2}`),
    }
)
