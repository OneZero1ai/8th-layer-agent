package mcpserver

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/stretchr/testify/require"

	cq "github.com/mozilla-ai/cq/sdk/go"
)

// writeCandidates marshals the given candidate list to a temp file in t.TempDir()
// and returns its absolute path. Mirrors how the /cq:reflect skill drops its
// candidate JSON to ~/.cache/cq/reflect-<ts>.json before calling the tool.
func writeCandidates(t *testing.T, candidates any) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "candidates.json")
	data, err := json.Marshal(candidates)
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(path, data, 0o600))
	return path
}

// decodeBatchFileResult parses the propose_batch_file tool response. Returns
// the raw JSON string alongside the struct so tests can additionally assert
// the absence of fields (e.g. summary, tier) without round-tripping through
// the typed shape, which would mask drop-out behavior.
func decodeBatchFileResult(t *testing.T, result *mcp.CallToolResult) (batchResponseCompact, string) {
	t.Helper()
	require.NotNil(t, result)
	require.False(t, result.IsError, "expected success, got error: %s", textOf(result))
	raw := textOf(result)
	var resp batchResponseCompact
	require.NoError(t, json.Unmarshal([]byte(raw), &resp))
	return resp, raw
}

func TestHandleProposeBatchFile(t *testing.T) {
	t.Parallel()

	t.Run("batch of 3 all clean returns 3 ids with compact shape", func(t *testing.T) {
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

		path := writeCandidates(t, []any{
			map[string]any{"summary": "a", "detail": "da", "action": "aa", "domains": []any{"x"}},
			map[string]any{"summary": "b", "detail": "db", "action": "ab", "domains": []any{"x", "y"}},
			map[string]any{"summary": "c", "detail": "dc", "action": "ac", "domains": []any{"z"}, "languages": []any{"go"}, "frameworks": []any{"cobra"}, "pattern": "cli"},
		})

		result, err := s.HandleProposeBatchFile(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name:      "propose_batch_file",
				Arguments: map[string]any{"candidates_path": path},
			},
		})
		require.NoError(t, err)

		resp, raw := decodeBatchFileResult(t, result)
		require.Len(t, resp.Stored, 3)
		require.Empty(t, resp.Errors)
		require.Equal(t, 3, calls)
		require.Equal(t, "ku_a", resp.Stored[0].ID)
		require.Equal(t, 0, resp.Stored[0].Index)
		require.Equal(t, "ku_c", resp.Stored[2].ID)
		require.Equal(t, 2, resp.Stored[2].Index)

		// Compact shape: per-stored entries MUST NOT carry summary or tier.
		// Re-parse the JSON loosely and assert the field set explicitly so a
		// regression that re-adds the verbose fields fails this test.
		var loose struct {
			Stored []map[string]any `json:"stored"`
		}
		require.NoError(t, json.Unmarshal([]byte(raw), &loose))
		require.Len(t, loose.Stored, 3)
		for i, entry := range loose.Stored {
			_, hasSummary := entry["summary"]
			_, hasTier := entry["tier"]
			require.False(t, hasSummary, "stored[%d] must not carry summary in compact response", i)
			require.False(t, hasTier, "stored[%d] must not carry tier in compact response", i)
			// And the expected fields ARE present.
			_, hasIndex := entry["index"]
			_, hasID := entry["id"]
			require.True(t, hasIndex, "stored[%d] should carry index", i)
			require.True(t, hasID, "stored[%d] should carry id", i)
		}
	})

	t.Run("mixed valid and invalid returns 1 stored + 1 error", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{proposeFn: kuFromParams("ku_x")}, "test")

		path := writeCandidates(t, []any{
			map[string]any{"summary": "good", "detail": "d", "action": "a", "domains": []any{"x"}},
			// missing detail — should fail validation
			map[string]any{"summary": "bad", "action": "a", "domains": []any{"x"}},
		})

		result, err := s.HandleProposeBatchFile(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name:      "propose_batch_file",
				Arguments: map[string]any{"candidates_path": path},
			},
		})
		require.NoError(t, err)

		resp, _ := decodeBatchFileResult(t, result)
		require.Len(t, resp.Stored, 1)
		require.Len(t, resp.Errors, 1)
		require.Equal(t, 0, resp.Stored[0].Index)
		require.Equal(t, 1, resp.Errors[0].Index)
		require.Equal(t, "bad", resp.Errors[0].Summary)
		require.Contains(t, resp.Errors[0].Error, "detail is required")
	})

	t.Run("file not found returns clean error including path", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{}, "test")
		missing := filepath.Join(t.TempDir(), "does-not-exist.json")

		result, err := s.HandleProposeBatchFile(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name:      "propose_batch_file",
				Arguments: map[string]any{"candidates_path": missing},
			},
		})
		require.NoError(t, err)
		require.True(t, result.IsError)
		require.Contains(t, textOf(result), "reading")
		require.Contains(t, textOf(result), missing)
	})

	t.Run("malformed JSON returns clean error naming the path", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{}, "test")
		path := filepath.Join(t.TempDir(), "bad.json")
		require.NoError(t, os.WriteFile(path, []byte("{not json at all"), 0o600))

		result, err := s.HandleProposeBatchFile(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name:      "propose_batch_file",
				Arguments: map[string]any{"candidates_path": path},
			},
		})
		require.NoError(t, err)
		require.True(t, result.IsError)
		require.Contains(t, textOf(result), path)
	})

	t.Run("empty path argument errors", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{}, "test")
		result, err := s.HandleProposeBatchFile(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name:      "propose_batch_file",
				Arguments: map[string]any{"candidates_path": ""},
			},
		})
		require.NoError(t, err)
		require.True(t, result.IsError)
		require.Contains(t, textOf(result), "candidates_path is required")
	})

	t.Run("missing candidates_path argument errors", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{}, "test")
		result, err := s.HandleProposeBatchFile(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name:      "propose_batch_file",
				Arguments: map[string]any{},
			},
		})
		require.NoError(t, err)
		require.True(t, result.IsError)
		require.Contains(t, textOf(result), "candidates_path is required")
	})

	t.Run("non-array JSON returns clean error", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{}, "test")
		path := filepath.Join(t.TempDir(), "obj.json")
		// Well-formed JSON, but an object — not an array of candidates.
		require.NoError(t, os.WriteFile(path, []byte(`{"summary":"a"}`), 0o600))

		result, err := s.HandleProposeBatchFile(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name:      "propose_batch_file",
				Arguments: map[string]any{"candidates_path": path},
			},
		})
		require.NoError(t, err)
		require.True(t, result.IsError)
		require.Contains(t, textOf(result), "candidates must be an array")
		require.Contains(t, textOf(result), path)
	})

	t.Run("quiet-fallback rollup yields response-level count + reason and no per-stored warning", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{
			proposeFn: func(_ context.Context, _ cq.ProposeParams) (cq.KnowledgeUnit, error) {
				return cq.KnowledgeUnit{}, &cq.FallbackError{
					LocalUnit: cq.KnowledgeUnit{ID: "ku_local", Tier: cq.Local},
					Err:       &cq.RemoteError{StatusCode: 401, Detail: "Invalid API key"},
				}
			},
		}, "test")

		path := writeCandidates(t, []any{
			map[string]any{"summary": "a", "detail": "d", "action": "a", "domains": []any{"x"}},
			map[string]any{"summary": "b", "detail": "d", "action": "a", "domains": []any{"x"}},
		})

		result, err := s.HandleProposeBatchFile(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name:      "propose_batch_file",
				Arguments: map[string]any{"candidates_path": path},
			},
		})
		require.NoError(t, err)

		resp, _ := decodeBatchFileResult(t, result)
		require.Len(t, resp.Stored, 2)
		require.Empty(t, resp.Errors)
		require.Equal(t, 2, resp.LocalFallbackCount)
		require.Contains(t, resp.LocalFallbackReason, "Invalid API key")
		for _, s := range resp.Stored {
			require.Empty(t, s.Warning, "quiet mode is default — per-stored warning must be empty")
		}
	})

	t.Run("propose error per-candidate does not abort batch", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{
			proposeFn: func(_ context.Context, params cq.ProposeParams) (cq.KnowledgeUnit, error) {
				if params.Summary == "fail" {
					return cq.KnowledgeUnit{}, errors.New("backend exploded")
				}
				return cq.KnowledgeUnit{ID: "ku_" + params.Summary, Tier: cq.Private}, nil
			},
		}, "test")

		path := writeCandidates(t, []any{
			map[string]any{"summary": "ok1", "detail": "d", "action": "a", "domains": []any{"x"}},
			map[string]any{"summary": "fail", "detail": "d", "action": "a", "domains": []any{"x"}},
			map[string]any{"summary": "ok2", "detail": "d", "action": "a", "domains": []any{"x"}},
		})

		result, err := s.HandleProposeBatchFile(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name:      "propose_batch_file",
				Arguments: map[string]any{"candidates_path": path},
			},
		})
		require.NoError(t, err)

		resp, _ := decodeBatchFileResult(t, result)
		require.Len(t, resp.Stored, 2)
		require.Len(t, resp.Errors, 1)
		require.Equal(t, 1, resp.Errors[0].Index)
		require.Contains(t, resp.Errors[0].Error, "backend exploded")
	})

	t.Run("empty candidate array returns empty response", func(t *testing.T) {
		t.Parallel()

		var calls int
		s := New(&mockClient{
			proposeFn: func(_ context.Context, _ cq.ProposeParams) (cq.KnowledgeUnit, error) {
				calls++
				return cq.KnowledgeUnit{}, nil
			},
		}, "test")

		path := writeCandidates(t, []any{})

		result, err := s.HandleProposeBatchFile(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{
				Name:      "propose_batch_file",
				Arguments: map[string]any{"candidates_path": path},
			},
		})
		require.NoError(t, err)

		resp, _ := decodeBatchFileResult(t, result)
		require.Empty(t, resp.Stored)
		require.Empty(t, resp.Errors)
		require.Equal(t, 0, calls)
	})
}
