import os
import shutil
from langchain.chains import ConversationalRetrievalChain
from langchain.retrievers import AmazonKendraRetriever
from langchain.prompts.prompt import PromptTemplate
from langchain.docstore.document import Document
from langchain.chains.summarize import load_summarize_chain
from langchain.memory import ConversationBufferMemory
from langchain import LLMChain
from langchain.llms import AmazonAPIGatewayBedrock
import json
import numpy as np
from model import *
from session import *



class SmartSearchQA:
    
    def init_cfg(self,
                 opensearch_index_name,
                 opensearch_user_name,
                 opensearch_user_password,
                 opensearch_or_kendra_host,
                 opensearch_port,
                 embedding_endpoint_name,
                 region,
                 llm_endpoint_name: str = 'pytorch-inference-llm-v1',
                 temperature: float = 0.01,
                 language: str = "chinese",
                 search_engine: str = "opensearch",
                 model_type:str = "normal",
                 bedrock_api_url:str = "",
                 bedrock_model_id:str="anthropic.claude-v2",
                 bedrock_max_tokens:int=500
                ):
        self.language = language
        self.search_engine = search_engine
        #add aos parmeter
        self.aos_index = opensearch_index_name
        self.aos_username = opensearch_user_name
        self.aos_passwd = opensearch_user_password
        self.aos_host = opensearch_or_kendra_host
        self.aos_port = opensearch_port

        
        if model_type == "llama2":
            self.llm = init_model_llama2(llm_endpoint_name,region,temperature)
        elif model_type == "bedrock_api":
            self.llm = AmazonAPIGatewayBedrock(api_url=bedrock_api_url)
            parameters={
                "modelId":bedrock_model_id,
                "max_tokens":bedrock_max_tokens,
                "temperature":temperature
            }
            self.llm.model_kwargs = parameters
        elif model_type == "bedrock":
            self.llm = init_model_bedrock(bedrock_model_id)
            parameters={
                "max_tokens":bedrock_max_tokens,
                "temperature":temperature
            }
            self.llm.model_kwargs = parameters
            
        else:
            self.llm = init_model(llm_endpoint_name,region,temperature)
        
        if self.search_engine == "opensearch":
            self.embeddings = init_embeddings(embedding_endpoint_name,region,self.language)
            self.vector_store = init_vector_store(self.embeddings,
                                                 opensearch_index_name,
                                                 opensearch_or_kendra_host,
                                                 opensearch_port,
                                                 opensearch_user_name,
                                                 opensearch_user_password)
        elif self.search_engine == "kendra":
            self.vector_store = None
            self.kendra_host = opensearch_or_kendra_host

    def get_qa_relation_score(self,query,answer):
        
        query_answer_emb = np.array(self.embeddings._embedding_func([query,answer]))
        query_emb = query_answer_emb[0]
        answer_emb = query_answer_emb[1]
        dot = query_emb * answer_emb 
        query_emb_len = np.linalg.norm(query_emb)
        answer_emb_len = np.linalg.norm(answer_emb)
        cos = dot.sum()/(query_emb_len * answer_emb_len)
        return cos

    def get_summarize(self,texts,
                        chain_type: str = "stuff",
                        prompt_template: str = "请根据{text}，总结一段摘要",
                        combine_prompt_template: str = "请根据{text}，总结一段摘要"
                        ):
                            
        texts = texts.split(';')
        texts_len = len(texts)
        print("texts len:",texts_len)
        
        PROMPT = PromptTemplate(template=prompt_template, input_variables=["text"])
        COMBINE_PROMPT = PromptTemplate(template=combine_prompt_template, input_variables=["text"])
        
        print('prompt:',PROMPT)
        
        if chain_type == "stuff":
            docs = [Document(page_content=t) for t in texts]
            chain = load_summarize_chain(self.llm, chain_type="stuff", prompt=PROMPT)
            result = chain.run(docs)
            
        else:
            new_texts = []
            num = 20
            for i in range(0,texts_len,num):
                if i + num < texts_len:
                    end = i + num
                else:
                    end = texts_len - i
                if len(texts[i:end]) > 0:
                    new_texts.append(";".join(texts[i:end]))
            docs = [Document(page_content=t) for t in new_texts]
            
            chain = load_summarize_chain(self.llm, 
                                         chain_type=chain_type, 
                                         map_prompt=PROMPT,
                                         combine_prompt=COMBINE_PROMPT)
            result = chain({"input_documents": docs}, return_only_outputs=True)
            result = result['output_text']
        
        return result

    def get_chat(self,query,language,prompt_template,table_name,session_id,model_type):
            
        prompt = PromptTemplate(
            input_variables=["history", "human_input"], 
            template=prompt_template
        )
        
        memory = ConversationBufferMemory(return_messages=True)
        session_info = ""
        if len(session_id) > 0:
            session_info = get_session_info(table_name,session_id)
            if len(session_info) > 0:
                for item in session_info:
                    print("session info:",item[0]," ; ",item[1]," ; ",item[2])
                    if item[2] == "chat":
                        memory.chat_memory.add_user_message(item[0])
                        memory.chat_memory.add_ai_message(item[1])
            
        chat_chain = LLMChain(
            llm=self.llm,
            prompt=prompt, 
            # verbose=True, 
            memory=memory,
        )
            
        output = chat_chain.predict(human_input=query)

        if len(session_id) > 0:
            update_session_info(table_name, session_id, query, output, "chat")
        
        return output
    
    def get_retriever(self,top_k):
        
        if self.search_engine == "opensearch":
            retriever = self.vector_store.as_retriever(search_kwargs={"k": top_k})
        elif self.search_engine == "kendra":
            retriever = AmazonKendraRetriever(index_id=self.kendra_host,top_k=top_k)
            
        return retriever
         

    def get_answer_from_conversational(self,query,
                                        session_id: str='',
                                        table_name: str='',
                                        language: str='chinese',
                                        prompt_template: str = "请根据{context}，回答{question}",
                                        condense_question_prompt: str="",
                                        top_k: int = 3,
                                        chain_type: str="stuff",
                                        search_method: str="vector",
                                        txt_docs_num: int=0,
                                        response_if_no_docs_found: str="",
                                        vec_docs_score_thresholds: float =0,
                                        txt_docs_score_thresholds: float =0,
                                        ):
        
        prompt = PromptTemplate(template=prompt_template,
                                input_variables=["context", "question"])
        combine_docs_chain_kwargs={"prompt":prompt}
        
        history = []
        session_info = ""
        if len(session_id) > 0 and len(table_name) > 0:
            session_info = get_session_info(table_name,session_id)
            if len(session_info) > 0:
                for item in session_info:
                    print("session info:",item[0]," ; ",item[1]," ; ",item[2])
                    if item[2] == "qa":
                        history.append((item[0],item[1]))
        
        print('history:',history)
        retriever = self.get_retriever(top_k)
        
        chain = ConversationalRetrievalChain.from_llm(
                    llm = self.llm,
                    chain_type=chain_type,
                    retriever=retriever,
                    condense_question_prompt = condense_question_prompt,
                    combine_docs_chain_kwargs = combine_docs_chain_kwargs,
                    return_source_documents = True,
                    return_generated_question = True
                )
        
#         result = chain({"question": query, "chat_history": history})
        result = chain({
            "question": query, 
            "chat_history": history, 
            "index_name": self.aos_index, 
            "aos_username": self.aos_username, 
            "aos_passwd": self.aos_passwd, 
            "aos_host": self.aos_host, 
            "aos_port": self.aos_port, 
            "search_engine": self.search_engine, 
            "search_method": search_method, 
            "txt_docs_num": txt_docs_num,
            'response_if_no_docs_found':response_if_no_docs_found,
            'vec_docs_score_thresholds':vec_docs_score_thresholds,
            'txt_docs_score_thresholds':txt_docs_score_thresholds
        })

        
        answer=result['answer']
        
        # answer=answer.split('\n\nhuman')[0].split('\n\n用户')[0].split('\n\nquestion')[0].split('\n\n\ufeffquestion')[0].split('\n\nQuestion')[0].strip()
        
        # if language == "english":
        #     answer = answer.split('Answer:')[-1]

        if len(session_id) > 0:
            new_query=result['generated_question']
            update_session_info(table_name, session_id, new_query, answer, "qa")
        
        return result
        

    def get_answer_from_conversational_llama2(self,query,
                                        session_id: str='',
                                        table_name: str='',
                                        prompt_template: str = "",
                                        condense_question_prompt: str="",
                                        top_k: int = 3,
                                        ):
        history = []
        session_info = ""
        if len(session_id) > 0 and len(table_name) > 0:
            session_info = get_session_info(table_name,session_id)
            if len(session_info) > 0:
                for item in session_info:
                    print("session info:",item[0]," ; ",item[1]," ; ",item[2])
                    if item[2] == "qa":
                        history.append((string_processor(item[0]),string_processor(item[1])))
        
        print('history:',history)
        
        new_question = string_processor(query)
        if len(history) > 0:
            for question,answer in history:
                condense_question_prompt += ('user:'+question+';')
                condense_question_prompt += ('assistant:'+answer+';')
        
            print('condense_question_prompt:',condense_question_prompt)
            
            query_trans = 'convert this question to be a standalone question:'+string_processor(query)
            prompt={'system_content':string_processor(condense_question_prompt),'query':string_processor(query_trans)}
            prompt_str = json.dumps(prompt)
            print('question trans system prompt',prompt_str)
            new_question_response = self.llm.predict(prompt_str)
            new_question = string_processor(new_question_response)
            print('new_question:',new_question)
        
        
        if self.search_engine == "opensearch":
            docs_with_scores = self.vector_store.similarity_search_with_score(new_question,k=top_k)
        elif self.search_engine == "kendra":
            docs_with_scores = retrieval_from_kendra(self.kendra_host,new_question,top_k)
        
        print('docs_with_scores:',docs_with_scores)
        docs = [doc[0] for doc in docs_with_scores]
        relate_docs=''
        for i in range(len(docs)):
            page_content = string_processor(docs[i].page_content)
            if len(page_content) > 0:
                relate_docs += (page_content + ';')
        
        prompt_template += relate_docs
        prompt={'system_content':string_processor(prompt_template),'history':history,'query':new_question}
        prompt_str = json.dumps(prompt)
        print('qa system prompt',prompt_str)
        answer = self.llm.predict(prompt_str)
        
        if len(session_id) > 0:
            update_session_info(table_name, session_id, new_question, string_processor(answer), "qa")
        
        response = {'answer':answer,'source_documents':docs_with_scores}
        return response

    def get_answer_from_chat_llama2(self,query,
                                    prompt_template: str = "",
                                    table_name: str='',
                                    session_id: str=''
                                    ):
        history = []
        session_info = ""
        if len(session_id) > 0 and len(table_name) > 0:
            session_info = get_session_info(table_name,session_id)
            if len(session_info) > 0:
                for item in session_info:
                    print("session info:",item[0]," ; ",item[1]," ; ",item[2])
                    if item[2] == "chat":
                        history.append((string_processor(item[0]),string_processor(item[1])))
        
        print('history:',history)
        
        prompt={'system_content':string_processor(prompt_template),'history':history,'query':string_processor(query)}
        prompt_str = json.dumps(prompt)
        print('chat system prompt',prompt_str)
        answer = self.llm.predict(prompt_str)
        
        if len(session_id) > 0:
            update_session_info(table_name, session_id, query, string_processor(answer), "chat")
        
        return answer