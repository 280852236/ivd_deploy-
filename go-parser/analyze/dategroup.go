package analyze

import (
	"regexp"
	"sort"
	"strings"
)

var reDateFn = regexp.MustCompile(`(\d{4}-\d{1,2}-\d{1,2})`)
var reDateContent = regexp.MustCompile(`(\d{4}[-/]\d{1,2}[-/]\d{1,2})`)

var sampleKw = map[string]bool{"样本空吸": true, "样本不足": true}
var reagentKw = map[string]bool{"试剂空吸": true, "试剂不足": true}
var faultKw = map[string]bool{"error": true, "fault": true, "异常": true, "故障": true, "报警": true, "失败": true}
var receiveKw = []string{"接收数据记录", "接收数据"}

func extractFileDate(fname string, fr *FileResult) string {
	m := reDateFn.FindString(fname)
	if m != "" {
		return m
	}
	return "未识别日期"
}

func classifyFile(fname string, fr *FileResult) []string {
	types := make(map[string]bool)
	ft := fr.FileType
	if ft != "" && ft != "unknown" && ft != "general" {
		types[ft] = true
	}
	if fr.HasFault {
		types["fault"] = true
	}
	if !types["sample"] && !types["reagent"] {
		for _, kw := range receiveKw {
			if strings.Contains(fname, kw) {
				types["receive"] = true
				break
			}
		}
	}
	for kw := range sampleKw {
		if strings.Contains(fname, kw) {
			types["sample"] = true
		}
	}
	for kw := range reagentKw {
		if strings.Contains(fname, kw) {
			types["reagent"] = true
		}
	}
	result := make([]string, 0, len(types))
	for t := range types {
		result = append(result, t)
	}
	sort.Strings(result)
	return result
}

func BuildDateGroupsAndSummary(files map[string]*FileResult) ([]DateGroup, Summary) {
	dateMap := make(map[string]map[string][]string)
	var faultCount, sampleCount, reagentCount, receiveCount int

	for fname, fdata := range files {
		fileDate := extractFileDate(fname, fdata)
		if _, ok := dateMap[fileDate]; !ok {
			dateMap[fileDate] = make(map[string][]string)
		}
		types := classifyFile(fname, fdata)
		dateMap[fileDate][fname] = types

		hasFault, hasSample, hasReagent, hasReceive := false, false, false, false
		for _, t := range types {
			switch t {
			case "fault":
				hasFault = true
			case "sample":
				hasSample = true
			case "reagent":
				hasReagent = true
			case "receive":
				hasReceive = true
			}
		}
		if hasFault && !hasSample && !hasReagent {
			faultCount++
		}
		if hasSample {
			sampleCount++
		}
		if hasReagent {
			reagentCount++
		}
		if hasReceive {
			receiveCount++
		}
	}

	dates := make([]string, 0, len(dateMap))
	for d := range dateMap {
		dates = append(dates, d)
	}
	sort.Sort(sort.Reverse(sort.StringSlice(dates)))

	groups := make([]DateGroup, 0, len(dates))
	for _, date := range dates {
		fnames := make([]string, 0, len(dateMap[date]))
		for fn := range dateMap[date] {
			fnames = append(fnames, fn)
		}
		sort.Strings(fnames)

	 fileList := make([]GroupFile, 0, len(fnames))
		for _, fname := range fnames {
			fr := files[fname]
			if fr == nil {
				continue
			}
			fileList = append(fileList, GroupFile{
				Name:               fname,
				Size:               fr.Size,
				IsCritical:         fr.IsCritical,
				Types:              dateMap[date][fname],
				HasFault:           fr.HasFault,
				IsAspirationFile:   fr.IsAspirationFile,
				HasAspirationMatch: fr.HasAspirationMatch,
			})
		}
		groups = append(groups, DateGroup{Date: date, Files: fileList})
	}

	return groups, Summary{
		Fault:   faultCount,
		Sample:  sampleCount,
		Reagent: reagentCount,
		Receive: receiveCount,
	}
}
