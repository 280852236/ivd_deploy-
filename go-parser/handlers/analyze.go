package handlers

import (
	"encoding/json"
	"io"
	"net/http"
	"runtime"
	"sync"
	"ivd_analyzer_go/analyze"
	"ivd_analyzer_go/matcher"
)

type AnalyzeRequest struct {
	ZipPath      string `json:"zip_path"`
	Series       string `json:"series"`
	Model        string `json:"model"`
	AnalysisType string `json:"analysis_type"`
}

type AnalyzeResponse struct {
	Files        map[string]*analyze.FileResult `json:"files"`
	FileMeta     []analyze.FileMeta             `json:"file_metadata"`
	DateGroups   []analyze.DateGroup            `json:"date_groups"`
	Summary      analyze.Summary                `json:"summary"`
	TotalFiles   int                            `json:"total_files"`
	TotalDates   int                            `json:"total_dates"`
	ZipProcessed int                            `json:"zip_processed"`
	FileCount    int                            `json:"file_count"`
	AspirationCount int                         `json:"aspiration_count"`
}

func AnalyzeHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req AnalyzeRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 10*1024*1024)).Decode(&req); err != nil {
		http.Error(w, "Invalid request: "+err.Error(), http.StatusBadRequest)
		return
	}

	if req.ZipPath == "" {
		http.Error(w, "zip_path is required", http.StatusBadRequest)
		return
	}

	metas, err := analyze.ExtractZipMetadata(req.ZipPath)
	if err != nil {
		http.Error(w, "Failed to extract metadata: "+err.Error(), http.StatusInternalServerError)
		return
	}

	files := make(map[string]*analyze.FileResult, len(metas))
	var aspirationMetas []analyze.FileMeta

	for _, meta := range metas {
		isAsp := analyze.IsAspirationFile(meta.FileType)
		files[meta.Name] = &analyze.FileResult{
			Name:             meta.Name,
			Size:             meta.Size,
			IsCritical:       meta.IsCritical,
			FileType:         meta.FileType,
			IsAspirationFile: isAsp,
			HasFault:         meta.FileType == "fault",
			NotLoaded:        true,
		}
		if isAsp {
			aspirationMetas = append(aspirationMetas, meta)
		}
	}

	quickScanAspiration(req.ZipPath, aspirationMetas, files, req.Series, req.Model)

	dateGroups, summary := analyze.BuildDateGroupsAndSummary(files)

	resp := AnalyzeResponse{
		Files:            files,
		FileMeta:         metas[:min(len(metas), 50)],
		DateGroups:       dateGroups,
		Summary:          summary,
		TotalFiles:       len(files),
		TotalDates:       len(dateGroups),
		ZipProcessed:     len(files),
		FileCount:        len(metas),
		AspirationCount:  len(aspirationMetas),
	}

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	json.NewEncoder(w).Encode(resp)
}

func quickScanAspiration(zipPath string, metas []analyze.FileMeta, files map[string]*analyze.FileResult, series, model string) {
	if len(metas) == 0 {
		return
	}

	workers := runtime.NumCPU()
	if workers > 4 {
		workers = 4
	}
	if workers > len(metas) {
		workers = len(metas)
	}

	type scanJob struct {
		meta analyze.FileMeta
	}

	jobs := make(chan scanJob, len(metas))
	results := make(chan struct {
		name      string
		hasMatch  bool
	}, len(metas))

	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for job := range jobs {
				if job.meta.Size > 5*1024*1024 {
					results <- struct {
						name      string
						hasMatch  bool
					}{job.meta.Name, false}
					continue
				}
				raw, err := analyze.ReadZipFile(zipPath, job.meta.RawName)
				if err != nil {
					results <- struct {
						name      string
						hasMatch  bool
					}{job.meta.Name, false}
					continue
				}
				content := string(raw)
				hasMatch := false
				lineCount := 0
				for _, line := range splitLines(content) {
					lineCount++
					if lineCount > 200 {
						break
					}
					asp := matcher.CheckAspirationAnomaly(line)
					if asp.Matched {
						hasMatch = true
						break
					}
				}
				results <- struct {
					name      string
					hasMatch  bool
				}{job.meta.Name, hasMatch}
			}
		}()
	}

	for _, meta := range metas {
		jobs <- scanJob{meta}
	}
	close(jobs)

	go func() {
		wg.Wait()
		close(results)
	}()

	for res := range results {
		if fr, ok := files[res.name]; ok {
			fr.HasAspirationMatch = res.hasMatch
		}
	}
}

func splitLines(s string) []string {
	var lines []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			lines = append(lines, s[start:i])
			start = i + 1
		}
	}
	if start < len(s) {
		lines = append(lines, s[start:])
	}
	return lines
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
