package main

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestContextCommentsExcludeHiddenAndUnverifiedGitHubComments(t *testing.T) {
	defaultTransport := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = defaultTransport })
	calls := 0
	http.DefaultTransport = testTransport(func(req *http.Request) (*http.Response, error) {
		calls++
		if req.Method != http.MethodPost || req.URL.String() != "https://api.github.com/graphql" {
			t.Fatalf("unexpected %s request to %s", req.Method, req.URL)
		}
		data, err := io.ReadAll(req.Body)
		if err != nil || !strings.Contains(string(data), "isMinimized") {
			t.Fatalf("adapter did not ask for visibility: %v", err)
		}
		var page string
		if calls%2 == 1 {
			page = `"pageInfo":{"hasNextPage":true,"endCursor":"next"},"nodes":[
				{"databaseId":1,"body":"public first","isMinimized":false},
				{"databaseId":2,"body":"hidden note","isMinimized":true}]`
		} else {
			page = `"pageInfo":{"hasNextPage":false},"nodes":[
				{"databaseId":3,"body":"public second","isMinimized":false},
				{"databaseId":4,"body":"unknown visibility"}]`
		}
		body := `{"data":{"repository":{"pullRequest":{"comments":{` + page + `}}}}}`
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body)), Request: req}, nil
	})
	in := request{Forge: "github", ProjectURL: "https://github.com/org/repo", Repository: "org/repo",
		Token: "fixture", Numbers: []int{7}}
	value, err := execute("context-comments", in)
	if err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if text := string(data); !strings.Contains(text, "public first") || !strings.Contains(text, "public second") ||
		strings.Contains(text, "hidden note") || strings.Contains(text, "unknown visibility") || calls != 2 {
		t.Fatalf("unexpected context comments after %d requests: %s", calls, text)
	}
}
