package main

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/medici-finance/assay/tools/desk/forgechange"
)

type testTransport func(*http.Request) (*http.Response, error)

func (f testTransport) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func TestChangeDescriptionUsesTheValidatedForgeResponse(t *testing.T) {
	head := strings.Repeat("a", 40)
	for _, fixture := range []struct {
		forge, project, endpoint, header, credential, payload string
	}{
		{
			"github", "https://github.com/org/repo", "https://api.github.com/repos/org/repo/pulls/7",
			"Authorization", "token fixture",
			`{"number":7,"state":"open","changed_files":2,"html_url":"https://github.com/org/repo/pull/7","head":{"sha":"%HEAD%"},"base":{"ref":"main"},"title":"Short title","body":"Acceptance criteria"}`,
		},
		{
			"gitlab", "https://gitlab.example/prefix/org/repo", "https://gitlab.example/prefix/api/v4/projects/org%2Frepo/merge_requests/7",
			"Private-Token", "fixture",
			`{"iid":7,"state":"opened","changes_count":"2","web_url":"https://gitlab.example/prefix/org/repo/-/merge_requests/7","sha":"%HEAD%","target_branch":"main","title":"Short title","description":"Acceptance criteria"}`,
		},
	} {
		t.Run(fixture.forge, func(t *testing.T) {
			calls := 0
			defaultTransport := http.DefaultTransport
			t.Cleanup(func() { http.DefaultTransport = defaultTransport })
			http.DefaultTransport = testTransport(func(req *http.Request) (*http.Response, error) {
				calls++
				if req.Method != http.MethodGet || req.URL.String() != fixture.endpoint || req.Header.Get(fixture.header) != fixture.credential {
					t.Fatalf("unexpected %s request to %s", req.Method, req.URL)
				}
				return &http.Response{StatusCode: 200, Header: make(http.Header),
					Body:    io.NopCloser(strings.NewReader(strings.ReplaceAll(fixture.payload, "%HEAD%", head))),
					Request: req}, nil
			})
			in := request{Forge: fixture.forge, ProjectURL: fixture.project, Repository: "org/repo",
				Token: "fixture", Numbers: []int{7}}
			value, err := execute("change-with-description", in)
			if err != nil {
				t.Fatal(err)
			}
			result, ok := value.(describedChange)
			if !ok || result.HeadSHA != head || result.BaseRef != "main" || result.ChangedFiles != 2 ||
				result.Title != "Short title" || result.Body != "Acceptance criteria" || calls != 1 {
				t.Fatalf("unexpected response %#v (%d calls)", value, calls)
			}
			regular, err := execute("change", in)
			if err != nil {
				t.Fatal(err)
			}
			encoded, err := json.Marshal(regular)
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(string(encoded), "Acceptance criteria") || calls != 2 {
				t.Fatalf("regular change exposed description: %s", encoded)
			}
		})
	}
}

func TestChangeReportsMetadataTruncation(t *testing.T) {
	capture := forgeCapture{}
	payload, err := json.Marshal(map[string]string{"title": "title", "body": strings.Repeat("a", maxChangeBody+1)})
	if err != nil {
		t.Fatal(err)
	}
	_, _ = capture.Write(payload)
	described := capture.describe(forgechange.Change{}, "github")
	if !described.DescriptionTruncated || len(described.Body) != maxChangeBody {
		t.Fatalf("bounded body length %d, truncated %t", len(described.Body), described.DescriptionTruncated)
	}
}

func TestDescriptionCaptureKeepsBounds(t *testing.T) {
	var capture forgeCapture
	chunk := []byte(strings.Repeat("a", maxCapturedResponse/2))
	for range 3 {
		if n, err := capture.Write(chunk); err != nil || n != len(chunk) {
			t.Fatalf("capture interfered with adapter body reads: %d, %v", n, err)
		}
	}
	if !capture.overflow || len(capture.payload) != 0 {
		t.Fatal("capture retained an oversized response")
	}
	text := boundedText("ab😀cd", 5)
	if text != "ab" || !utf8.ValidString(text) {
		t.Fatalf("truncation split a UTF-8 character: %q", text)
	}
}
