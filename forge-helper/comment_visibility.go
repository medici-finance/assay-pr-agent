package main

import "encoding/json"

// The adapter validates and walks the GitHub change thread. Its public comment
// type omits isMinimized, so keep only IDs whose visibility the same pages confirm.
type commentVisibility map[int64]bool

func (visible commentVisibility) record(payload []byte) {
	var page struct {
		Data struct {
			Repository struct {
				PullRequest struct {
					Comments struct {
						Nodes []struct {
							ID        int64 `json:"databaseId"`
							Minimized *bool `json:"isMinimized"`
						}
					}
				}
			}
		}
	}
	if json.Unmarshal(payload, &page) != nil {
		return
	}
	for _, node := range page.Data.Repository.PullRequest.Comments.Nodes {
		if node.ID < 1 {
			continue
		}
		show := node.Minimized != nil && !*node.Minimized
		if earlier, seen := visible[node.ID]; seen {
			show = earlier && show
		}
		visible[node.ID] = show
	}
}
