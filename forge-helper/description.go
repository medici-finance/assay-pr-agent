package main

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"unicode/utf8"

	"github.com/medici-finance/assay/tools/desk/forgechange"
)

const (
	maxChangeResponse = 256 * 1024
	maxChangeTitle    = 512
	maxChangeBody     = 16 * 1024
)

type describedChange struct {
	forgechange.Change
	Title string `json:"title,omitempty"`
	Body  string `json:"body,omitempty"`
}

// The pinned adapter validates the change's identity and already parses this
// response. Capture its description without changing the request or the body
// it reads. An oversized response still reaches the adapter in full.
type changeCapture struct {
	next     http.RoundTripper
	payload  []byte
	overflow bool
}

type capturedBody struct {
	io.Reader
	io.Closer
}

func (c *changeCapture) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := c.next.RoundTrip(req)
	c.payload, c.overflow = nil, false
	if err == nil && resp.StatusCode == http.StatusOK {
		resp.Body = capturedBody{Reader: io.TeeReader(resp.Body, c), Closer: resp.Body}
	}
	return resp, err
}

func (c *changeCapture) Write(data []byte) (int, error) {
	if len(data) > maxChangeResponse-len(c.payload) {
		c.payload, c.overflow = nil, true
	}
	if !c.overflow {
		c.payload = append(c.payload, data...)
	}
	return len(data), nil
}

func (c *changeCapture) describe(change forgechange.Change, forge string) describedChange {
	result := describedChange{Change: change}
	var fields struct {
		Title       string `json:"title"`
		Body        string `json:"body"`
		Description string `json:"description"`
	}
	if c.overflow || json.Unmarshal(c.payload, &fields) != nil {
		return result
	}
	if forge == "gitlab" {
		fields.Body = fields.Description
	}
	result.Title = boundedText(fields.Title, maxChangeTitle)
	result.Body = boundedText(fields.Body, maxChangeBody)
	return result
}

func boundedText(text string, limit int) string {
	text = strings.TrimSpace(text)
	if len(text) <= limit {
		return text
	}
	for !utf8.RuneStart(text[limit]) {
		limit--
	}
	return strings.TrimSpace(text[:limit])
}
