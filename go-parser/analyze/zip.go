package analyze

import (
	"archive/zip"
	"io"
	"os"
	"path/filepath"
	"strings"
)

var textExtensions = map[string]bool{
	".txt": true, ".log": true, ".md": true, ".csv": true,
}

var criticalKeywords = []string{"样本空吸", "试剂空吸", "故障代码", "error", "fault"}

func decodeZipName(raw string) string {
	for i := 0; i < len(raw); i++ {
		if raw[i] > 127 {
			return raw
		}
	}
	return raw
}

func ExtractZipMetadata(zipPath string) ([]FileMeta, error) {
	r, err := zip.OpenReader(zipPath)
	if err != nil {
		return nil, err
	}
	defer r.Close()

	var metas []FileMeta
	for _, f := range r.File {
		if f.FileInfo().IsDir() {
			continue
		}
		name := decodeZipName(f.Name)
		if strings.Contains(name, "..") || strings.HasPrefix(name, "/") || filepath.IsAbs(name) {
			continue
		}
		ext := strings.ToLower(filepath.Ext(name))
		if !textExtensions[ext] {
			continue
		}
		if f.UncompressedSize64 > 10*1024*1024 {
			continue
		}
		metas = append(metas, FileMeta{
			Name:       name,
			RawName:    f.Name,
			Size:       int64(f.UncompressedSize64),
			IsCritical: isCritical(name),
			FileType:   detectFileType(name),
		})
	}
	return metas, nil
}

func isCritical(name string) bool {
	lower := strings.ToLower(name)
	for _, kw := range criticalKeywords {
		if strings.Contains(lower, strings.ToLower(kw)) {
			return true
		}
	}
	return false
}

func detectFileType(name string) string {
	lower := strings.ToLower(name)
	switch {
	case strings.Contains(lower, "样本空吸") || strings.Contains(lower, "样本不足"):
		return "sample"
	case strings.Contains(lower, "试剂空吸") || strings.Contains(lower, "试剂不足"):
		return "reagent"
	case strings.Contains(lower, "接收数据"):
		return "receive"
	case strings.Contains(lower, "故障") || strings.Contains(lower, "error") || strings.Contains(lower, "fault"):
		return "fault"
	default:
		return "general"
	}
}

func ReadZipFile(zipPath, rawName string) ([]byte, error) {
	r, err := zip.OpenReader(zipPath)
	if err != nil {
		return nil, err
	}
	defer r.Close()

	for _, f := range r.File {
		if f.Name == rawName {
			rc, err := f.Open()
			if err != nil {
				return nil, err
			}
			defer rc.Close()
			return io.ReadAll(rc)
		}
	}
	return nil, os.ErrNotExist
}

func IsAspirationFile(fileType string) bool {
	return fileType == "sample" || fileType == "reagent"
}
