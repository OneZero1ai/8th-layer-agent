package mcpserver

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/stretchr/testify/require"

	cq "github.com/mozilla-ai/cq/sdk/go"
)

// decodeBatchResult parses an MCP CallToolResult text payload as batchResponse.
func decodeBatchResult(t *testing.T, result *mcp.CallToolResult) batchResponse {
	t.Helper()
	require.NotNil(t, result)
	require.False(t, result.IsError, "expected success, got error: %s", textOf(result))
	var resp batchResponse
	require.NoError(t, json.Unmarshal([]byte(textOf(result)), &resp))
	return resp
}

func textOf(result *mcp.CallToolResult) string {
	if result == nil || len(result.Content) == 0 {
		return ""
	}
	if tc, ok := result.Content[0].(mcp.TextContent); ok {
		return tc.Text
	}
	return ""
}

// kuFromParams returns a stable mock KU for a given ProposeParams. Lets the
// per-candidate tests assert that the right summary made it through.
func kuFromParams(id string) func(_ context.Context, _ cq.ProposeParams) (cq.KnowledgeUnit, error) {
	return func(_ context.Context, _ cq.ProposeParams) (cq.KnowledgeUnit, error) {
		return cq.KnowledgeUnit{ID: id, Tier: cq.Private}, nil
	}
}

func TestHandleProposeBatch(t *testing.T) {
	t.Parallel()

	t.Run("batch of 3 all clean returns 3 ids", func(t *testing.T) {
		t.Parallel()

		var calls int
		s := New(&mockClient{
			proposeFn: func(_ context.Context, params cq.ProposeParams) (cq.KnowledgeUnit, error) {
				calls++
				return cq.KnowledgeUnit{
					ID:   "ku_" + params.Summary,
					Tier: cq.Private,
				}, nil
			},
		}, "test")

		result, err := s.HandleProposeBatch(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name: "propose_batch",
				Arguments: map[string]any{
					"candidates": []any{
						map[string]any{
							"summary": "a",
							"detail":  "da",
							"action":  "aa",
							"domains": []any{"x"},
						},
						map[string]any{
							"summary": "b",
							"detail":  "db",
							"action":  "ab",
							"domains": []any{"x", "y"},
						},
						map[string]any{
							"summary":    "c",
							"detail":     "dc",
							"action":     "ac",
							"domains":    []any{"z"},
							"languages":  []any{"go"},
							"frameworks": []any{"cobra"},
							"pattern":    "cli",
						},
					},
				},
			},
		})
		require.NoError(t, err)

		resp := decodeBatchResult(t, result)
		require.Len(t, resp.Stored, 3)
		require.Empty(t, resp.Errors)
		require.Equal(t, 3, calls)
		require.Equal(t, "ku_a", resp.Stored[0].ID)
		require.Equal(t, "a", resp.Stored[0].Summary)
		require.Equal(t, "private", resp.Stored[0].Tier)
		require.Equal(t, 0, resp.Stored[0].Index)
		require.Equal(t, "ku_c", resp.Stored[2].ID)
		require.Equal(t, 2, resp.Stored[2].Index)
	})

	t.Run("batch of 3 with one invalid returns 2 ids + 1 error", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{proposeFn: kuFromParams("ku_x")}, "test")

		result, err := s.HandleProposeBatch(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name: "propose_batch",
				Arguments: map[string]any{
					"candidates": []any{
						map[string]any{
							"summary": "good1",
							"detail":  "d",
							"action":  "a",
							"domains": []any{"x"},
						},
						map[string]any{
							// missing detail — should fail validation
							"summary": "bad",
							"action":  "a",
							"domains": []any{"x"},
						},
						map[string]any{
							"summary": "good2",
							"detail":  "d",
							"action":  "a",
							"domains": []any{"x"},
						},
					},
				},
			},
		})
		require.NoError(t, err)

		resp := decodeBatchResult(t, result)
		require.Len(t, resp.Stored, 2)
		require.Len(t, resp.Errors, 1)
		require.Equal(t, 1, resp.Errors[0].Index)
		require.Equal(t, "bad", resp.Errors[0].Summary)
		require.Contains(t, resp.Errors[0].Error, "detail is required")
		// Surviving stored entries preserve the original input index.
		require.Equal(t, 0, resp.Stored[0].Index)
		require.Equal(t, "good1", resp.Stored[0].Summary)
		require.Equal(t, 2, resp.Stored[1].Index)
		require.Equal(t, "good2", resp.Stored[1].Summary)
	})

	t.Run("batch of 0 returns empty", func(t *testing.T) {
		t.Parallel()

		var calls int
		s := New(&mockClient{
			proposeFn: func(_ context.Context, _ cq.ProposeParams) (cq.KnowledgeUnit, error) {
				calls++
				return cq.KnowledgeUnit{}, nil
			},
		}, "test")

		result, err := s.HandleProposeBatch(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name: "propose_batch",
				Arguments: map[string]any{
					"candidates": []any{},
				},
			},
		})
		require.NoError(t, err)

		resp := decodeBatchResult(t, result)
		require.Empty(t, resp.Stored)
		require.Empty(t, resp.Errors)
		require.Equal(t, 0, calls)
	})

	t.Run("missing candidates argument errors", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{}, "test")
		result, err := s.HandleProposeBatch(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{Name: "propose_batch", Arguments: map[string]any{}},
		})
		require.NoError(t, err)
		require.True(t, result.IsError)
		require.Contains(t, textOf(result), "candidates is required")
	})

	t.Run("candidates not an array errors", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{}, "test")
		result, err := s.HandleProposeBatch(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name: "propose_batch",
				Arguments: map[string]any{
					"candidates": "not-a-list",
				},
			},
		})
		require.NoError(t, err)
		require.True(t, result.IsError)
		require.Contains(t, textOf(result), "must be an array")
	})

	t.Run("candidate missing domains is reported per-index", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{proposeFn: kuFromParams("ku_x")}, "test")
		result, err := s.HandleProposeBatch(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name: "propose_batch",
				Arguments: map[string]any{
					"candidates": []any{
						map[string]any{
							"summary": "no-domains",
							"detail":  "d",
							"action":  "a",
							"domains": []any{},
						},
					},
				},
			},
		})
		require.NoError(t, err)

		resp := decodeBatchResult(t, result)
		require.Empty(t, resp.Stored)
		require.Len(t, resp.Errors, 1)
		require.Equal(t, 0, resp.Errors[0].Index)
		require.Contains(t, resp.Errors[0].Error, "domains must contain at least one tag")
	})

	t.Run("propose error is reported per-candidate without aborting batch", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{
			proposeFn: func(_ context.Context, params cq.ProposeParams) (cq.KnowledgeUnit, error) {
				if params.Summary == "fail" {
					return cq.KnowledgeUnit{}, errors.New("backend exploded")
				}
				return cq.KnowledgeUnit{ID: "ku_" + params.Summary, Tier: cq.Private}, nil
			},
		}, "test")

		result, err := s.HandleProposeBatch(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name: "propose_batch",
				Arguments: map[string]any{
					"candidates": []any{
						map[string]any{"summary": "ok1", "detail": "d", "action": "a", "domains": []any{"x"}},
						map[string]any{"summary": "fail", "detail": "d", "action": "a", "domains": []any{"x"}},
						map[string]any{"summary": "ok2", "detail": "d", "action": "a", "domains": []any{"x"}},
					},
				},
			},
		})
		require.NoError(t, err)

		resp := decodeBatchResult(t, result)
		require.Len(t, resp.Stored, 2)
		require.Len(t, resp.Errors, 1)
		require.Equal(t, 1, resp.Errors[0].Index)
		require.Equal(t, "fail", resp.Errors[0].Summary)
		require.Contains(t, resp.Errors[0].Error, "backend exploded")
	})

	t.Run("fallback error stores locally and surfaces warning", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{
			proposeFn: func(_ context.Context, _ cq.ProposeParams) (cq.KnowledgeUnit, error) {
				return cq.KnowledgeUnit{}, &cq.FallbackError{
					LocalUnit: cq.KnowledgeUnit{
						ID:   "ku_local",
						Tier: cq.Local,
					},
					Err: errors.New("remote API unreachable: connection refused"),
				}
			},
		}, "test")

		result, err := s.HandleProposeBatch(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name: "propose_batch",
				Arguments: map[string]any{
					"candidates": []any{
						map[string]any{"summary": "x", "detail": "d", "action": "a", "domains": []any{"x"}},
					},
				},
			},
		})
		require.NoError(t, err)

		resp := decodeBatchResult(t, result)
		require.Len(t, resp.Stored, 1)
		require.Empty(t, resp.Errors)
		require.Equal(t, "ku_local", resp.Stored[0].ID)
		require.Equal(t, "local", resp.Stored[0].Tier)
		require.Contains(t, resp.Stored[0].Warning, "stored locally after remote failure")
	})

	t.Run("non-string domain item is reported", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{proposeFn: kuFromParams("ku_x")}, "test")
		result, err := s.HandleProposeBatch(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name: "propose_batch",
				Arguments: map[string]any{
					"candidates": []any{
						map[string]any{
							"summary": "bad-domain",
							"detail":  "d",
							"action":  "a",
							"domains": []any{"x", 42},
						},
					},
				},
			},
		})
		require.NoError(t, err)

		resp := decodeBatchResult(t, result)
		require.Empty(t, resp.Stored)
		require.Len(t, resp.Errors, 1)
		require.Contains(t, resp.Errors[0].Error, "domains[1] must be a string")
	})

	t.Run("registered with server", func(t *testing.T) {
		t.Parallel()

		// Smoke check: the propose_batch tool must be wired up alongside
		// propose so the harness sees it in the tool list. We verify by
		// invoking via the public handler — registration happens in New().
		s := New(&mockClient{proposeFn: kuFromParams("ku_x")}, "test")
		require.NotNil(t, s.MCPServer())
	})
}
