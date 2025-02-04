import traceback

import litellm

from ..terminal_interface.utils.display_markdown_message import display_markdown_message
from .utils.html_to_base64 import html_to_base64
from .utils.merge_deltas import merge_deltas
from .utils.truncate_output import truncate_output


def respond(interpreter):
    """
    Yields tokens, but also adds them to interpreter.messages. TBH probably would be good to seperate those two responsibilities someday soon
    Responds until it decides not to run any more code or say anything else.
    """

    last_unsupported_code = ""

    while True:
        system_message = interpreter.generate_system_message()

        # Create message object
        system_message = {"role": "system", "message": system_message}

        # Create the version of messages that we'll send to the LLM
        messages_for_llm = interpreter.messages.copy()
        messages_for_llm = [system_message] + messages_for_llm

        # It's best to explicitly tell these LLMs when they don't get an output
        for message in messages_for_llm:
            if "output" in message and message["output"] == "":
                message["output"] = "No output"

        ### RUN THE LLM ###

        # Add a new message from the assistant to interpreter's "messages" attribute
        # (This doesn't go to the LLM. We fill this up w/ the LLM's response)
        interpreter.messages.append({"role": "assistant"})

        # Start putting chunks into the new message
        # + yielding chunks to the user
        try:
            # Track the type of chunk that the coding LLM is emitting
            chunk_type = None

            for chunk in interpreter._llm(messages_for_llm):
                # Add chunk to the last message
                interpreter.messages[-1] = merge_deltas(interpreter.messages[-1], chunk)

                # This is a coding llm
                # It will yield dict with either a message, language, or code (or language AND code)

                # We also want to track which it's sending to we can send useful flags.
                # (otherwise pretty much everyone needs to implement this)
                for new_chunk_type in ["message", "language", "code"]:
                    if new_chunk_type in chunk and chunk_type != new_chunk_type:
                        if chunk_type != None:
                            yield {f"end_of_{chunk_type}": True}
                        # Language is actually from a code block
                        if new_chunk_type == "language":
                            new_chunk_type = "code"
                        chunk_type = new_chunk_type
                        yield {f"start_of_{chunk_type}": True}

                yield chunk

            # We don't trigger the end_of_message or end_of_code flag if we actually end on either (we just exit the loop above)
            if chunk_type:
                yield {f"end_of_{chunk_type}": True}

        except litellm.exceptions.BudgetExceededError:
            display_markdown_message(
                f"""> Max budget exceeded

                **Session spend:** ${litellm._current_cost}
                **Max budget:** ${interpreter.max_budget}

                Press CTRL-C then run `interpreter --max_budget [higher USD amount]` to proceed.
            """
            )
            break
        # Provide extra information on how to change API keys, if we encounter that error
        # (Many people writing GitHub issues were struggling with this)
        except Exception as e:
            if (
                interpreter.local == False
                and "auth" in str(e).lower()
                or "api key" in str(e).lower()
            ):
                output = traceback.format_exc()
                raise Exception(
                    f"{output}\n\nThere might be an issue with your API key(s).\n\nTo reset your API key (we'll use OPENAI_API_KEY for this example, but you may need to reset your ANTHROPIC_API_KEY, HUGGINGFACE_API_KEY, etc):\n        Mac/Linux: 'export OPENAI_API_KEY=your-key-here',\n        Windows: 'setx OPENAI_API_KEY your-key-here' then restart terminal.\n\n"
                )
            elif (
                interpreter.local == False
                and "access" in str(e).lower()
            ):
                response = input(
                    f"  You do not have access to {interpreter.model}. Would you like to try gpt-3.5-turbo instead? (y/n)\n\n  "
                )
                print("")  # <- Aesthetic choice

                if response.strip().lower() == "y":
                    interpreter.model = "gpt-3.5-turbo-1106"
                    interpreter.context_window = 16000
                    interpreter.max_tokens = 4096
                    interpreter.function_calling_llm = True
                    display_markdown_message(f"> Model set to `{interpreter.model}`")
                else:
                    raise Exception("\n\nYou will need to add a payment method and purchase credits for the OpenAI api billing page (different from ChatGPT) to use gpt-4\nLink: https://platform.openai.com/account/billing/overview")
            elif interpreter.local:
                raise Exception(
                    str(e)
                    + """

Please make sure LM Studio's local server is running by following the steps above.

If LM Studio's local server is running, please try a language model with a different architecture.

                    """
                )
            else:
                raise

        ### RUN CODE (if it's there) ###

        if "code" in interpreter.messages[-1]:
            if interpreter.debug_mode:
                print("Running code:", interpreter.messages[-1])

            try:
                # What language/code do you want to run?
                language = interpreter.messages[-1]["language"].lower().strip()
                code = interpreter.messages[-1]["code"]

                # Is this language enabled/supported?
                if language not in interpreter.languages:
                    output = f"`{language}` disabled or not supported."

                    yield {"output": output}
                    interpreter.messages[-1]["output"] = output

                    # Let the response continue so it can deal with the unsupported code in another way. Also prevent looping on the same piece of code.
                    if code != last_unsupported_code:
                        last_unsupported_code = code
                        continue
                    else:
                        break

                # Fix a common error where the LLM thinks it's in a Jupyter notebook
                if language == "python" and code.startswith("!"):
                    code = code[1:]
                    interpreter.messages[-1]["code"] = code
                    interpreter.messages[-1]["language"] = "shell"

                # Yield a message, such that the user can stop code execution if they want to
                try:
                    yield {"executing": {"code": code, "language": language}}
                except GeneratorExit:
                    # The user might exit here.
                    # We need to tell python what we (the generator) should do if they exit
                    break

                yield {"start_of_output": True}

                # Yield each line, also append it to last messages' output
                interpreter.messages[-1]["output"] = ""
                for line in interpreter.computer.run(language, code):
                    yield line
                    if "output" in line:
                        output = interpreter.messages[-1]["output"]
                        output += "\n" + line["output"]

                        # Truncate output
                        output = truncate_output(output, interpreter.max_output)

                        interpreter.messages[-1]["output"] = output.strip()
                    # Vision
                    if interpreter.vision:
                        base64_image = None
                        if "image" in line:
                            base64_image = line["image"]
                        if "html" in line:
                            base64_image = html_to_base64(line["html"])

                        if base64_image:
                            yield {"output": "Sending image output to GPT-4V..."}
                            interpreter.messages[-1][
                                "image"
                            ] = f"data:image/jpeg;base64,{base64_image}"

            except:
                output = traceback.format_exc()
                yield {"output": output.strip()}
                interpreter.messages[-1]["output"] = output.strip()

            yield {"active_line": None}
            yield {"end_of_output": True}

        else:
            # Doesn't want to run code. We're done!
            break

    return
