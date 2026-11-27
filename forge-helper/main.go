// pr-agent-forge is the JSON bridge between the Python agent and Assay's Go
// library. Each invocation receives its credentials and target on stdin.
package main

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"slices"

	"github.com/medici-finance/assay/tools/desk/forgechange"
)

type request struct {
	Forge, ProjectURL, Repository, Token, GitLabLoopback string
	GitLabCAFile                                         string
	AppID, InstallationID, PrivateKey                    string
	Numbers                                              []int
	Publication                                          forgechange.Publication
}

type thread struct {
	Number   int                   `json:"number"`
	Comments []forgechange.Comment `json:"comments"`
}

func execute(verb string, in request) (any, error) {
	if verb == "mint" {
		return forgechange.MintGitHubToken(nil, in.AppID, in.InstallationID, in.Repository, []byte(in.PrivateKey))
	}
	var transport *http.Client
	if in.GitLabLoopback != "" {
		ca, err := os.ReadFile(in.GitLabCAFile)
		if err != nil {
			return nil, errors.New("cannot read the GitLab proxy CA")
		}
		roots := x509.NewCertPool()
		if !roots.AppendCertsFromPEM(ca) {
			return nil, errors.New("invalid GitLab proxy CA")
		}
		rt := http.DefaultTransport.(*http.Transport).Clone()
		rt.TLSClientConfig = &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}
		transport = &http.Client{Transport: rt}
	}
	var capture *forgeCapture
	visibility := make(commentVisibility)
	if verb == "change-with-description" || (verb == "context-comments" && in.Forge == "github") {
		if transport == nil {
			transport = &http.Client{Transport: http.DefaultTransport}
		}
		capture = &forgeCapture{next: transport.Transport}
		if verb == "context-comments" {
			capture.onPage = visibility.record
		}
		transport.Transport = capture
	}
	client, err := forgechange.New(forgechange.Config{
		Forge: in.Forge, ProjectURL: in.ProjectURL, Repository: in.Repository,
		Token: in.Token, GitLabLoopback: in.GitLabLoopback, HTTPClient: transport,
	})
	if err != nil {
		return nil, err
	}
	switch verb {
	case "open":
		numbers, capped, err := client.OpenChanges()
		return struct {
			Numbers []int `json:"numbers"`
			Capped  bool  `json:"capped"`
		}{numbers, capped}, err
	case "comments", "context-comments":
		if verb == "context-comments" && (in.Forge != "github" || len(in.Numbers) != 1) {
			return nil, errors.New("expected one GitHub change for context comments")
		}
		out := struct {
			Items   []thread `json:"items"`
			Partial []int    `json:"partial"`
		}{Items: []thread{}}
		for _, n := range in.Numbers {
			rows, err := client.Comments(n)
			if err != nil {
				fmt.Fprintf(os.Stderr, "change %d: %v\n", n, err)
				out.Partial = append(out.Partial, n)
				continue
			}
			if verb == "context-comments" {
				rows = slices.DeleteFunc(rows, func(row forgechange.Comment) bool {
					return !visibility[row.DatabaseID]
				})
			}
			out.Items = append(out.Items, thread{n, rows})
		}
		if len(out.Items) == 0 && len(out.Partial) != 0 {
			return nil, errors.New("all change comment reads failed")
		}
		return out, nil
	case "change", "change-with-description", "change-files":
		if len(in.Numbers) != 1 {
			return nil, errors.New("expected one change number")
		}
		if verb != "change-files" {
			change, err := client.Change(in.Numbers[0])
			if err != nil || capture == nil {
				return change, err
			}
			return capture.describe(change, in.Forge), nil
		}
		return client.ChangedFiles(in.Numbers[0])
	case "check-publication":
		// Use the same visibility, head, and content checks as comment publication.
		// The checked-only path never sends a comment.
		in.Publication.AlreadyPosted = true
		return nil, client.PublishComment(in.Publication)
	case "comment":
		err := client.PublishComment(in.Publication)
		return nil, err
	default:
		return nil, errors.New("unknown forge operation")
	}
}

func run(args []string, input io.Reader, output io.Writer) error {
	if len(args) != 1 {
		return errors.New("expected one operation")
	}
	var in request
	dec := json.NewDecoder(io.LimitReader(input, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&in); err != nil {
		return errors.New("invalid forge request")
	}
	if err := dec.Decode(new(any)); err != io.EOF {
		return errors.New("expected one forge request")
	}
	result, err := execute(args[0], in)
	if err != nil {
		return err
	}
	return json.NewEncoder(output).Encode(result)
}

func main() {
	if err := run(os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
