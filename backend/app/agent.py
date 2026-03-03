"""
NL2SQL Agent — placeholder module.

TODO: Replace the stub below with the real LangGraph agent implementation.

Integration checklist
---------------------
1. Import the LLM and tools:

       from langchain_openai import ChatOpenAI
       from langchain_community.utilities import SQLDatabase
       from langchain_community.agent_toolkits import SQLDatabaseToolkit
       from langgraph.prebuilt import create_react_agent

2. Build the SQLDatabase connection using settings.database_url:

       db = SQLDatabase.from_uri(settings.database_url)

3. Build the toolkit and extract tools:

       llm = ChatOpenAI(model="gpt-4o", temperature=0, api_key=settings.openai_api_key)
       toolkit = SQLDatabaseToolkit(db=db, llm=llm)
       tools = toolkit.get_tools()

4. Create the agent with memory / checkpointer for thread-level history:

       from langgraph.checkpoint.memory import MemorySaver
       checkpointer = MemorySaver()
       agent = create_react_agent(llm, tools, checkpointer=checkpointer)

5. Replace `run_agent` below to invoke the real agent and extract the
   SQL query and natural-language answer from its output messages.

6. Optionally wire in a ChromaDB vector store for few-shot SQL examples:

       from langchain_community.vectorstores import Chroma
       from langchain_openai import OpenAIEmbeddings
       # store = Chroma(embedding_function=OpenAIEmbeddings(...), ...)
"""

import logging
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def run_agent(question: str, thread_id: str) -> dict[str, str]:
    """
    Execute the NL2SQL agent for a given question and conversational thread.

    Args:
        question:  Natural-language question from the user.
        thread_id: Unique session identifier used by LangGraph's checkpointer
                   to maintain per-thread conversation history.

    Returns:
        A dict with keys ``answer`` (str) and ``sql`` (str).

    TODO: Replace this stub with the real create_react_agent invocation.
          See the module docstring above for the full integration checklist.
    """
    logger.info("run_agent called | thread_id=%s | question=%r", thread_id, question)

    # TODO: remove placeholder and invoke the LangGraph agent here.
    # Example invocation once the agent is built:
    #
    #   config = {"configurable": {"thread_id": thread_id}}
    #   result = await agent.ainvoke({"messages": [("user", question)]}, config=config)
    #   answer = result["messages"][-1].content
    #   sql = extract_sql_from_messages(result["messages"])
    #   return {"answer": answer, "sql": sql}

    return {
        "answer": (
            f"[Placeholder] Agent not yet implemented. "
            f'Received question: "{question}"'
        ),
        "sql": "-- TODO: generated SQL will appear here",
    }
