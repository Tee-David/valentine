# Valentine v2 — Agent Testing Guide

Manual test scenarios for each agent. Send these messages to your Telegram bot and verify the expected behavior.

---

## 1. ZeroClaw (Intent Router)

Tests that ZeroClaw correctly routes messages to the right agent.

| # | Send This | Expected Route | What to Check |
|---|-----------|---------------|---------------|
| 1.1 | `Hello, how are you?` | Oracle | Casual conversational reply |
| 1.2 | `Write me a Python function to reverse a linked list` | CodeSmith | Code-formatted response |
| 1.3 | `Search for the latest SpaceX launch news` | Oracle | Response with web search results |
| 1.4 | `Generate an image of a sunset over Tokyo` | Iris | Returns an image |
| 1.5 | *(send a voice note saying "what time is it")* | Echo | Transcribes and responds |
| 1.6 | *(send a photo of a receipt)* with caption `Read the text on this` | Iris | OCR extraction of receipt text |
| 1.7 | `What's the weather in Lagos?` | Nexus | Weather tool call + response |
| 1.8 | `Explain quantum computing in simple terms` | Oracle | Educational explanation |
| 1.9 | `Create a bash script that monitors CPU usage` | CodeSmith | Shell script in response |
| 1.10 | `What's the price of Bitcoin?` | Nexus | Crypto price tool call |

---

## 2. Oracle (Research & Reasoning)

### Basic Chat
| # | Send This | Expected |
|---|-----------|----------|
| 2.1 | `What is the capital of Burkina Faso?` | Ouagadougou — factual answer |
| 2.2 | `Explain the difference between TCP and UDP` | Technical comparison |
| 2.3 | `Tell me a joke` | Responds with humor |

### Web Search
| # | Send This | Expected |
|---|-----------|----------|
| 2.4 | `Search for top programming languages in 2026` | Response with DuckDuckGo results cited |
| 2.5 | `Search for Nigerian tech startup funding news` | Current search results with sources |
| 2.6 | `Search for how to deploy a Python app on Oracle Cloud` | Tutorial-style results |

### URL Fetching
| # | Send This | Expected |
|---|-----------|----------|
| 2.7 | `Summarize this: https://en.wikipedia.org/wiki/Artificial_intelligence` | Summary of the Wikipedia page |
| 2.8 | `What does this page say? https://docs.python.org/3/whatsnew/3.12.html` | Summary of Python 3.12 changelog |

### Multi-Step Reasoning
| # | Send This | Expected |
|---|-----------|----------|
| 2.9 | `Compare the pros and cons of PostgreSQL vs MongoDB for a chat application with 10M users` | Detailed analytical comparison |
| 2.10 | `If I invest $1000 monthly at 8% annual return for 20 years, what will I have? Show the math.` | Calculated answer with reasoning |
| 2.11 | `I'm building a SaaS product. Should I use microservices or a monolith? My team is 3 developers.` | Contextual architecture advice |

---

## 3. CodeSmith (Code & DevOps)

### Code Generation
| # | Send This | Expected |
|---|-----------|----------|
| 3.1 | `Write a Python FastAPI endpoint that accepts a JSON body with name and email, validates them, and returns a greeting` | Working FastAPI code |
| 3.2 | `Write a JavaScript function that debounces any callback with a configurable delay` | Debounce implementation |
| 3.3 | `Create a Dockerfile for a Node.js 20 app with multi-stage build` | Valid Dockerfile |

### Shell Commands
| # | Send This | Expected |
|---|-----------|----------|
| 3.4 | `Run: echo "Hello from Valentine"` | Command output: Hello from Valentine |
| 3.5 | `Run: ls -la /tmp/valentine/workspace` | Directory listing |
| 3.6 | `Run: python3 -c "print(sum(range(1, 101)))"` | Output: 5050 |

### File Operations
| # | Send This | Expected |
|---|-----------|----------|
| 3.7 | `Create a file called hello.py in workspace with a hello world Flask app` | Confirms file written |
| 3.8 | `Read the file hello.py from workspace` | Shows file contents |

### Complex Tasks
| # | Send This | Expected |
|---|-----------|----------|
| 3.9 | `Write a complete Python script that reads a CSV file, calculates the average of a numeric column, and outputs the result. Include error handling.` | Full script with try/except |
| 3.10 | `Debug this code: def fib(n): return fib(n-1) + fib(n-2)` | Identifies missing base case, provides fix |
| 3.11 | `Write a bash one-liner that finds all .log files older than 7 days and deletes them` | `find . -name "*.log" -mtime +7 -delete` or similar |

### Security (Should Block)
| # | Send This | Expected |
|---|-----------|----------|
| 3.12 | `Run: rm -rf /` | Blocked by denylist |
| 3.13 | `Run: shutdown now` | Blocked by denylist |

---

## 4. Iris (Vision & Image)

### Image Generation
| # | Send This | Expected |
|---|-----------|----------|
| 4.1 | `Generate an image of a cyberpunk city at night with neon lights` | Returns generated image |
| 4.2 | `Generate an image of a cute robot holding flowers` | Returns generated image |
| 4.3 | `Create a logo for a tech company called "NovaByte" — minimalist, blue and white` | Returns generated logo-style image |

### Image Analysis
| # | Send This | Expected |
|---|-----------|----------|
| 4.4 | *(send a photo of food)* `What dish is this?` | Identifies the food |
| 4.5 | *(send a photo of a city skyline)* `What city is this?` | Attempts to identify the city |
| 4.6 | *(send a photo of code on a screen)* `What does this code do?` | Analyzes visible code |

### OCR
| # | Send This | Expected |
|---|-----------|----------|
| 4.7 | *(send a photo of a handwritten note)* `Read the text` | Extracts handwritten text |
| 4.8 | *(send a screenshot of an error message)* `What error is this?` | Reads and explains the error |

### Screenshot-to-Code
| # | Send This | Expected |
|---|-----------|----------|
| 4.9 | *(send a screenshot of a simple login form UI)* `Convert this UI to HTML/CSS` | Detailed structural description of the UI |
| 4.10 | *(send a screenshot of a dashboard)* `Describe this interface for a developer to rebuild` | Technical UI breakdown |

---

## 5. Echo (Voice)

### Voice-In Text-Out
| # | Send This | Expected |
|---|-----------|----------|
| 5.1 | *(send a voice note)* saying "Hello Valentine, how are you?" | Transcribes + responds with text AND voice |
| 5.2 | *(send a voice note)* saying "What is the square root of 144?" | Transcribes, responds with "12" |
| 5.3 | *(send a voice note)* saying "Search for the latest iPhone model" | Transcribes, re-routes to Oracle via ZeroClaw |

### Voice-In Voice-Out
| # | Send This | Expected |
|---|-----------|----------|
| 5.4 | *(send a voice note)* saying "Tell me a short joke" | Returns a voice message with the joke |
| 5.5 | *(send a voice note)* saying "Good morning" | Returns a voice greeting |

### Edge Cases
| # | Send This | Expected |
|---|-----------|----------|
| 5.6 | *(send a very short voice note — 1 second of silence)* | Handles gracefully (empty transcript error or minimal response) |
| 5.7 | *(send a 30+ second voice note with a detailed question)* | Full transcription + relevant response |

---

## 6. Cortex (Memory)

### Memory Storage (Implicit)
| # | Send This | Expected |
|---|-----------|----------|
| 6.1 | `My name is David and I'm a software developer from Lagos` | Oracle responds conversationally; Cortex silently stores facts |
| 6.2 | `I prefer Python over JavaScript for backend work` | Response + memory stored |
| 6.3 | `I'm currently working on a project called Valentine` | Response + memory stored |

### Memory Recall (via Context Injection)
| # | Send This (after tests above) | Expected |
|---|-------------------------------|----------|
| 6.4 | `What's my name?` | Should recall "David" from stored memory |
| 6.5 | `What programming language do I prefer?` | Should recall "Python" preference |
| 6.6 | `What project am I working on?` | Should recall "Valentine" |

**Note:** Memory recall depends on ZeroClaw's `_fetch_context` being wired to Cortex. If not yet connected, these will fail gracefully — Oracle will say it doesn't know.

---

## 7. Nexus (API Tools)

### Weather Tool
| # | Send This | Expected |
|---|-----------|----------|
| 7.1 | `What's the weather in London?` | Calls get_weather tool, returns result (mock data) |
| 7.2 | `Is it hot in Dubai right now?` | Routes to Nexus, weather tool response |

### Crypto Tool
| # | Send This | Expected |
|---|-----------|----------|
| 7.3 | `What's the current price of Ethereum?` | Calls get_crypto_price, returns ETH price (mock data) |
| 7.4 | `How much is Bitcoin worth?` | BTC price response |
| 7.5 | `Price of SOL?` | SOL price response |

### Unknown Tool
| # | Send This | Expected |
|---|-----------|----------|
| 7.6 | `Book me a flight to Paris` | No matching tool — explains it can't do that yet |
| 7.7 | `Send an email to john@example.com` | No matching tool — explains limitation |

---

## 8. End-to-End / Integration

### Multi-Agent Chaining
| # | Send This | Expected |
|---|-----------|----------|
| 8.1 | *(send a screenshot of a website)* `Rebuild this in HTML` | Iris analyzes → description → (ideally chains to CodeSmith) |
| 8.2 | *(send a voice note)* saying "Write me a Python hello world" | Echo transcribes → re-routes to ZeroClaw → CodeSmith handles |

### Fallback Behavior
| # | Send This | Expected |
|---|-----------|----------|
| 8.3 | `asdfghjkl` | Routes to Oracle (fallback), responds conversationally |
| 8.4 | *(send only an emoji: thumbs up)* | Handles gracefully, doesn't crash |
| 8.5 | *(send an empty message or just spaces)* | Handles gracefully |

### Rapid Messages (Stress)
| # | Action | Expected |
|---|--------|----------|
| 8.6 | Send 5 messages rapidly in a row | All get processed, no crashes, responses arrive |
| 8.7 | Send a text, then immediately a photo, then a voice note | Each routes to correct agent, all respond |

### Long Content
| # | Send This | Expected |
|---|-----------|----------|
| 8.8 | Paste a 2000-character essay and say `Summarize this` | Oracle summarizes without truncation errors |
| 8.9 | `Write a 500-line Python project with multiple files for a REST API` | CodeSmith produces substantial output (may be truncated at 4096 chars) |

---

## 9. Error Handling

| # | Scenario | Expected |
|---|----------|----------|
| 9.1 | Kill Redis container, send a message, restart Redis | Error message to user, then recovery after restart |
| 9.2 | Send a message with an invalid media file path | Agent returns error gracefully, no crash |
| 9.3 | Check health endpoint during normal operation | `curl http://127.0.0.1:8080/health` returns all "up" |
| 9.4 | Kill one agent process, wait 10 seconds | Supervisor auto-restarts it, health returns to "ok" |

---

## Quick Smoke Test (Run These First)

If you want a fast check that everything works, run these 5 tests in order:

1. **Text → Oracle:** `Hey Valentine, what's 2 + 2?`
2. **Text → CodeSmith:** `Write a Python function that checks if a string is a palindrome`
3. **Text → Nexus:** `What's the price of Bitcoin?`
4. **Photo → Iris:** *(send any photo)* `Describe this image`
5. **Voice → Echo:** *(send a voice note)* saying anything

If all 5 return responses, your system is working end-to-end.
