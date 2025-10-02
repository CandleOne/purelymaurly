// Simple span macro function
function createSpan(text, className = "") {
    const span = document.createElement("span");
    span.textContent = text;
    if (className) span.className = className;
    return span;
}

// Example usage:
// document.body.appendChild(createSpan("Hello, world!", "highlight"));