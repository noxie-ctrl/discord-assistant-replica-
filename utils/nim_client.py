import os
import asyncio
import traceback
import aiohttp

NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"


def build_system_prompt(profile: dict) -> str:
    return (
        f"You are {profile.get('name', 'Lucy')}, a {profile.get('age', '21')}-year-old "
        f"({profile.get('pronouns', 'she/her')}) AI who lives in this Discord server as: "
        f"{profile.get('role', 'a helpful assistant')}.\n"
        f"Personality traits: {profile.get('traits', '')}\n"
        f"Backstory: {profile.get('backstory', '')}\n"
        f"Speaking style: {profile.get('speaking_style', '')}\n"
        f"Boundaries: {profile.get('boundaries', '')}\n"
        "Stay fully in character. Keep replies conversational and not too long "
        "(usually 1-4 sentences) unless the user clearly wants something longer/detailed."
    )


async def get_ai_reply(profile: dict, history: list, user_message: str) -> str:
    api_key = (os.getenv("NVIDIA_API_KEY") or "").strip()
    model = (os.getenv("NIM_MODEL") or "meta/llama-3.3-70b-instruct").strip()

    if not api_key:
        return "(Lucy's brain isn't connected yet — ask my owner to set NVIDIA_API_KEY.)"

    messages = [{"role": "system", "content": build_system_prompt(profile)}]
    for turn in history:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_message})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.8,
        "top_p": 0.9,
        "max_tokens": 400,
    }

    timeout = aiohttp.ClientTimeout(total=45)
    try:
        print(f"[NIM] Calling {NIM_URL} with model={model!r}, key_len={len(api_key)}")
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(NIM_URL, headers=headers, json=payload) as resp:
                print(f"[NIM] Response status: {resp.status}")
                if resp.status != 200:
                    text = await resp.text()
                    print(f"[NIM] Error body: {text[:500]}")
                    return f"(hmm, my brain hiccuped — {resp.status}: {text[:300]})"
                data = await resp.json()
                return data["choices"][0]["message"]["content"].strip()
    except asyncio.TimeoutError:
        print("[NIM] Request timed out after 45s")
        return "(my brain took too long to respond — the NIM API might be slow or the model name might be wrong. Check NIM_MODEL.)"
    except Exception as e:
        print(f"[NIM] Exception: {type(e).__name__}: {e}")
        traceback.print_exc()
        return f"(something went wrong talking to my AI brain: {type(e).__name__}: {e})"