/**
 * Thin re-export so consumers import a hook (idiomatic) rather than the
 * raw context. Keeps the API tight: `useTour()` returns the state +
 * actions, nothing else.
 */

export { useTourContext as useTour } from "./TourProvider"
