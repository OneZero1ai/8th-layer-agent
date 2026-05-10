package mcpserver

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/mark3labs/mcp-go/mcp"

	cq "github.com/mozilla-ai/cq/sdk/go"
)

// ProposeBatchTool returns the MCP tool definition for propose_batch.
//
// propose_batch accepts a list of candidate knowledge units (each shaped
// like a single propose call) and stores them in one MCP round-trip. The
// existence of a single-call surface is what makes /cq:reflect viable in
// hot sessions: the harness echoes one tool-call signature + one JSON
// response instead of N of each, capping reflect output at ~10 lines
// regardless of candidate count.
//
// Partial success is allowed — each candidate is attempted independently,
// and the response carries a `stored` slice of successes (id, summary,
// tier) plus an `errors` slice for failures (index, summary, error).
func ProposeBatchTool() mcp.Tool {
	candidateSchema := map[string]any{
		"type": "object",
		"properties": map[string]any{
			"summary": map[string]any{
				"type":        "string",
				"description": "Brief summary of the insight.",
			},
			"detail": map[string]any{
				"type":        "string",
				"description": "Detailed explanation of what was discovered.",
			},
			"action": map[string]any{
				"type":        "string",
				"description": "Recommended action for agents encountering this situation.",
			},
			"domains": map[string]any{
				"type":        "array",
				"items":       map[string]any{"type": "string"},
				"description": "Domain tags for this knowledge.",
			},
			"languages": map[string]any{
				"type":        "array",
				"items":       map[string]any{"type": "string"},
				"description": "Programming language context.",
			},
			"frameworks": map[string]any{
				"type":        "array",
				"items":       map[string]any{"type": "string"},
				"description": "Framework context.",
			},
			"pattern": map[string]any{
				"type":        "string",
				"description": "Pattern name.",
			},
		},
		"required": []string{"summary", "detail", "action", "domains"},
	}

	return mcp.NewTool("propose_batch",
		mcp.WithDescription(
			"Propose multiple knowledge units in a single call. "+
				"Use this for batch flows like /cq:reflect to keep tool-output noise bounded. "+
				"Each candidate has the same shape as the propose tool's arguments. "+
				"Partial success is allowed: failures are reported per-candidate and do not abort the batch.",
		),
		mcp.WithArray("candidates",
			mcp.Required(),
			mcp.Description("Candidate knowledge units to propose. Each item carries the same fields as a single propose call."),
			mcp.Items(candidateSchema),
		),
	)
}

// batchStored describes a successfully proposed candidate in the batch response.
type batchStored struct {
	Index   int    `json:"index"`
	ID      string `json:"id"`
	Summary string `json:"summary"`
	Tier    string `json:"tier"`
	// Warning is set when the unit was stored locally after a remote
	// failure (cq.FallbackError). Empty when the propose succeeded cleanly.
	Warning string `json:"warning,omitempty"`
}

// batchError describes a failed candidate in the batch response.
type batchError struct {
	Index   int    `json:"index"`
	Summary string `json:"summary,omitempty"`
	Error   string `json:"error"`
}

// batchResponse is the JSON shape returned to the MCP caller.
type batchResponse struct {
	Stored []batchStored `json:"stored"`
	Errors []batchError  `json:"errors"`
}

// HandleProposeBatch creates multiple knowledge units from a single MCP call.
//
//nolint:gocyclo // The handler is straightforward; per-candidate validation drives the branching.
func (s *Server) HandleProposeBatch(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	args := req.GetArguments()
	rawCandidates, ok := args["candidates"]
	if !ok {
		return mcp.NewToolResultError("candidates is required"), nil
	}

	candList, ok := rawCandidates.([]any)
	if !ok {
		return mcp.NewToolResultError("candidates must be an array"), nil
	}

	resp := batchResponse{
		Stored: []batchStored{},
		Errors: []batchError{},
	}

	for i, raw := range candList {
		cand, ok := raw.(map[string]any)
		if !ok {
			resp.Errors = append(resp.Errors, batchError{
				Index: i,
				Error: "candidate must be an object",
			})
			continue
		}

		summary, sErr := requireBatchString(cand, "summary")
		if sErr != "" {
			resp.Errors = append(resp.Errors, batchError{Index: i, Error: sErr})
			continue
		}
		detail, sErr := requireBatchString(cand, "detail")
		if sErr != "" {
			resp.Errors = append(resp.Errors, batchError{Index: i, Summary: summary, Error: sErr})
			continue
		}
		action, sErr := requireBatchString(cand, "action")
		if sErr != "" {
			resp.Errors = append(resp.Errors, batchError{Index: i, Summary: summary, Error: sErr})
			continue
		}

		domains, sErr := requireBatchStringSlice(cand, "domains")
		if sErr != "" {
			resp.Errors = append(resp.Errors, batchError{Index: i, Summary: summary, Error: sErr})
			continue
		}
		if len(domains) == 0 {
			resp.Errors = append(resp.Errors, batchError{
				Index:   i,
				Summary: summary,
				Error:   "domains must contain at least one tag",
			})
			continue
		}

		languages, sErr := optionalBatchStringSlice(cand, "languages")
		if sErr != "" {
			resp.Errors = append(resp.Errors, batchError{Index: i, Summary: summary, Error: sErr})
			continue
		}
		frameworks, sErr := optionalBatchStringSlice(cand, "frameworks")
		if sErr != "" {
			resp.Errors = append(resp.Errors, batchError{Index: i, Summary: summary, Error: sErr})
			continue
		}
		pattern, sErr := optionalBatchString(cand, "pattern")
		if sErr != "" {
			resp.Errors = append(resp.Errors, batchError{Index: i, Summary: summary, Error: sErr})
			continue
		}

		params := cq.ProposeParams{
			Summary:    summary,
			Detail:     detail,
			Action:     action,
			Domains:    domains,
			Languages:  languages,
			Frameworks: frameworks,
			Pattern:    pattern,
		}

		ku, err := s.client.Propose(ctx, params)
		var fb *cq.FallbackError
		if errors.As(err, &fb) {
			resp.Stored = append(resp.Stored, batchStored{
				Index:   i,
				ID:      fb.LocalUnit.ID,
				Summary: summary,
				Tier:    string(fb.LocalUnit.Tier),
				Warning: fb.Error(),
			})
			continue
		}
		if err != nil {
			resp.Errors = append(resp.Errors, batchError{
				Index:   i,
				Summary: summary,
				Error:   err.Error(),
			})
			continue
		}

		resp.Stored = append(resp.Stored, batchStored{
			Index:   i,
			ID:      ku.ID,
			Summary: summary,
			Tier:    string(ku.Tier),
		})
	}

	data, err := json.Marshal(resp)
	if err != nil {
		return nil, fmt.Errorf("encoding batch result: %w", err)
	}

	return mcp.NewToolResultText(string(data)), nil
}

// requireBatchString extracts a required string field from a candidate map.
// Returns the value, or an error message if missing/wrong type.
func requireBatchString(cand map[string]any, key string) (string, string) {
	raw, ok := cand[key]
	if !ok {
		return "", fmt.Sprintf("%s is required", key)
	}
	s, ok := raw.(string)
	if !ok {
		return "", fmt.Sprintf("%s must be a string", key)
	}
	if s == "" {
		return "", fmt.Sprintf("%s must not be empty", key)
	}
	return s, ""
}

// optionalBatchString extracts an optional string field from a candidate map.
func optionalBatchString(cand map[string]any, key string) (string, string) {
	raw, ok := cand[key]
	if !ok || raw == nil {
		return "", ""
	}
	s, ok := raw.(string)
	if !ok {
		return "", fmt.Sprintf("%s must be a string", key)
	}
	return s, ""
}

// requireBatchStringSlice extracts a required []string field from a candidate map.
func requireBatchStringSlice(cand map[string]any, key string) ([]string, string) {
	raw, ok := cand[key]
	if !ok {
		return nil, fmt.Sprintf("%s is required", key)
	}
	return coerceStringSlice(raw, key)
}

// optionalBatchStringSlice extracts an optional []string field from a candidate map.
func optionalBatchStringSlice(cand map[string]any, key string) ([]string, string) {
	raw, ok := cand[key]
	if !ok || raw == nil {
		return nil, ""
	}
	return coerceStringSlice(raw, key)
}

// coerceStringSlice converts an arbitrary JSON-decoded value into []string.
// JSON arrays decode into []any, so each element is type-asserted in turn.
func coerceStringSlice(raw any, key string) ([]string, string) {
	arr, ok := raw.([]any)
	if !ok {
		// Tolerate the case where the caller already passed []string.
		if direct, ok := raw.([]string); ok {
			return direct, ""
		}
		return nil, fmt.Sprintf("%s must be an array of strings", key)
	}
	out := make([]string, 0, len(arr))
	for j, item := range arr {
		s, ok := item.(string)
		if !ok {
			return nil, fmt.Sprintf("%s[%d] must be a string", key, j)
		}
		out = append(out, s)
	}
	return out, ""
}
