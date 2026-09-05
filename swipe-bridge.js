(function () {
  "use strict";

  const minimumDistance = 56;
  const maximumDuration = 650;
  const minimumVelocity = 0.25;
  const axisRatio = 1.35;
  const directionLockDistance = 12;
  let gesture = null;

  function resetGesture() {
    gesture = null;
  }

  window.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1) {
      resetGesture();
      return;
    }
    const touch = event.touches[0];
    gesture = {
      id: touch.identifier,
      startX: touch.clientX,
      startY: touch.clientY,
      startedAt: performance.now()
    };
  }, { capture: true, passive: true });

  window.addEventListener("touchmove", (event) => {
    if (!gesture || event.touches.length !== 1) {
      resetGesture();
      return;
    }
    const touch = event.touches[0];
    if (touch.identifier !== gesture.id) {
      resetGesture();
      return;
    }
    const horizontalDistance = Math.abs(touch.clientX - gesture.startX);
    const verticalDistance = Math.abs(touch.clientY - gesture.startY);
    if (
      verticalDistance >= directionLockDistance
      && verticalDistance > horizontalDistance * axisRatio
    ) {
      resetGesture();
    }
  }, { capture: true, passive: true });

  window.addEventListener("touchend", (event) => {
    if (!gesture || event.touches.length) {
      resetGesture();
      return;
    }
    const touch = Array.from(event.changedTouches)
      .find((candidate) => candidate.identifier === gesture.id);
    if (!touch) {
      resetGesture();
      return;
    }

    const horizontalDistance = touch.clientX - gesture.startX;
    const verticalDistance = touch.clientY - gesture.startY;
    const elapsed = performance.now() - gesture.startedAt;
    const horizontalSpeed = Math.abs(horizontalDistance) / Math.max(elapsed, 1);
    resetGesture();

    if (
      Math.abs(horizontalDistance) < minimumDistance
      || elapsed > maximumDuration
      || horizontalSpeed < minimumVelocity
      || Math.abs(horizontalDistance) < Math.abs(verticalDistance) * axisRatio
    ) {
      return;
    }

    window.parent.postMessage({
      type: "ramen-bench:swipe",
      direction: horizontalDistance < 0 ? 1 : -1
    }, "*");
  }, { capture: true, passive: true });

  window.addEventListener("touchcancel", resetGesture, { capture: true, passive: true });
}());
